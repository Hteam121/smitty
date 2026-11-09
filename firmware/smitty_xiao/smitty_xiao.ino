// Smitty Wi-Fi audio bridge for the Seeed XIAO ESP32S3 on the reSpeaker XVF3800.
//
//   XVF3800 (I2S) <-> XIAO ESP32S3 <-> Wi-Fi WebSocket <-> Smitty on the Mac
//
// Mic path:  I2S RX 16 kHz / 32-bit / stereo from the XVF3800 (left = processed
//            voice beam) -> take left channel, >>16 to int16 -> 20 ms binary
//            WebSocket frames (640 bytes) to ws://<mac>:8766/audio
// Speaker:   binary frames from the server (int16 mono 16 kHz) -> ring buffer ->
//            I2S TX as 32-bit stereo (same sample on both channels) -> XVF3800
//            DAC -> amp -> speaker.   Text {"type":"stop"} flushes the ring buffer.
//
// Build:  arduino-cli compile -b esp32:esp32:XIAO_ESP32S3 firmware/smitty_xiao
//         arduino-cli upload  -b esp32:esp32:XIAO_ESP32S3 -p /dev/cu.usbmodem101 firmware/smitty_xiao
// Needs:  esp32 core 3.x, library "WebSockets" (Markus Sattler). Edit config.h.
//
// XVF3800 firmware: use respeaker_xvf3800_i2s_slave_v1.0.9_16k.bin with
// I2S_ROLE_MASTER below (ESP32 drives BCLK/WS), or the master_48k image with
// I2S_ROLE_SLAVE and SAMPLE_RATE 48000 (not what Smitty expects; keep 16 kHz).

#include <WiFi.h>
#include <Wire.h>
#include <ESPmDNS.h>
#include <ArduinoOTA.h>
#include <WebSocketsClient.h>
#include <driver/i2s_std.h>
#include "config.h"

// ---- XIAO <-> XVF3800 I2S pins (from Seeed's XVF3800 + XIAO examples) ----
#define PIN_BCLK   8
#define PIN_WS     7
#define PIN_DOUT   44   // ESP32 -> XVF3800 (playback)
#define PIN_DIN    43   // XVF3800 -> ESP32 (mic)

#define SAMPLE_RATE      16000
#define PACKET_SAMPLES   320                      // 20 ms
#define RING_SAMPLES     (SAMPLE_RATE * 2)        // 2 s of playback buffer

// ---- XVF3800 control over I2C (same resource/command ids as the USB tool) ----
#define XMOS_ADDR 0x2C
static bool xmos_read(uint8_t resid, uint8_t cmd, uint8_t* out, uint8_t n) {
  for (int attempt = 0; attempt < 20; attempt++) {
    Wire.beginTransmission(XMOS_ADDR);
    Wire.write(resid); Wire.write(cmd | 0x80); Wire.write(n + 1);
    if (Wire.endTransmission() != 0) return false;
    if (Wire.requestFrom((uint8_t)XMOS_ADDR, (uint8_t)(n + 1)) < n + 1) return false;
    uint8_t status = Wire.read();
    for (uint8_t i = 0; i < n; i++) out[i] = Wire.read();
    if (status == 0) return true;
    if (status != 64) return false;
    delay(10);
  }
  return false;
}
static bool xmos_write(uint8_t resid, uint8_t cmd, const uint8_t* data, uint8_t n) {
  Wire.beginTransmission(XMOS_ADDR);
  Wire.write(resid); Wire.write(cmd); Wire.write(n);
  for (uint8_t i = 0; i < n; i++) Wire.write(data[i]);
  return Wire.endTransmission() == 0;
}
// which 7-bit addresses ACK on the XIAO's I2C bus (the XVF3800 should be at 0x2C)
static String i2c_scan() {
  String found = "";
  for (uint8_t a = 0x08; a < 0x78; a++) {
    Wire.beginTransmission(a);
    if (Wire.endTransmission() == 0) { if (found.length()) found += ","; found += "0x" + String(a, HEX); }
  }
  return found.length() ? found : String("none");
}

// last values read from the XVF3800, also sent to the server in the stat message
static uint8_t x_ver[3] = {0}, x_inactive = 0xFF, x_gpo[5] = {0};
static bool x_ok = false;
static void xmos_report() {
  bool ok_v = xmos_read(48, 0, x_ver, 3);
  bool ok_i = xmos_read(35, 24, &x_inactive, 1);    // I2S_INACTIVE: 0 = active
  bool ok_g = xmos_read(20, 0, x_gpo, 5);           // GPO: X0D11, X0D30(mute), X0D31(amp en, low=on), X0D33(led pwr), X0D39
  x_ok = ok_v;
  Serial.printf("[xmos] version %s%u.%u.%u  i2s_inactive=%s%u  gpo(mute=%u amp_en=%u ledpwr=%u) %s\n",
                ok_v ? "" : "(no reply) ", x_ver[0], x_ver[1], x_ver[2], ok_i ? "" : "? ", x_inactive,
                x_gpo[1], x_gpo[2], x_gpo[3], ok_g ? "" : "(gpo read failed)");
}

static i2s_chan_handle_t tx_chan = nullptr;
static i2s_chan_handle_t rx_chan = nullptr;
static WebSocketsClient ws;
static String server_ip;                                 // resolved SERVER_HOST

// SERVER_HOST may be an IP or a Bonjour name like "my-mac.local"; resolve the latter.
static String resolve_server(int attempts = 10) {
  String host = SERVER_HOST;
  if (!host.endsWith(".local")) return host;
  String name = host.substring(0, host.length() - 6);
  for (int attempt = 0; attempt < attempts; attempt++) {
    IPAddress ip = MDNS.queryHost(name, 2000);
    if (ip != IPAddress(0, 0, 0, 0)) {
      Serial.printf("[mdns] %s -> %s\n", host.c_str(), ip.toString().c_str());
      return ip.toString();
    }
    Serial.printf("[mdns] %s not found yet (try %d)\n", host.c_str(), attempt + 1);
    if (attempts > 1) delay(1000);
  }
  return server_ip.length() ? server_ip : host;                                           // let the socket fail loudly
}

// playback ring buffer (int16 mono), filled from Wi-Fi, drained by the I2S task
static int16_t ring[RING_SAMPLES];
static volatile uint32_t ring_w = 0, ring_r = 0;
static portMUX_TYPE ring_mux = portMUX_INITIALIZER_UNLOCKED;
static volatile bool connected = false;
static uint32_t rx_packets = 0, tx_packets = 0;

// mic packets cross from the audio task (core 1) to loop() (core 0) through a
// queue: the WebSockets library must only be touched from the task that runs ws.loop().
struct MicPacket { int16_t s[PACKET_SAMPLES]; uint16_t n; };
static QueueHandle_t mic_q;
// raw I2S diagnostics (per 5 s): are any bits arriving from the XVF3800?
static volatile int32_t raw_min_l = 0, raw_max_l = 0, raw_min_r = 0, raw_max_r = 0;
static volatile uint32_t raw_frames = 0, raw_zero_frames = 0, mic_dropped = 0;

static inline uint32_t ring_count() { return (ring_w - ring_r); }

static void ring_push(const int16_t* s, size_t n) {
  portENTER_CRITICAL(&ring_mux);
  for (size_t i = 0; i < n; i++) {
    if (ring_count() >= RING_SAMPLES) ring_r++;          // overflow: drop oldest
    ring[ring_w % RING_SAMPLES] = s[i];
    ring_w++;
  }
  portEXIT_CRITICAL(&ring_mux);
}

static size_t ring_pop(int16_t* out, size_t n) {
  portENTER_CRITICAL(&ring_mux);
  size_t got = 0;
  while (got < n && ring_count() > 0) { out[got++] = ring[ring_r % RING_SAMPLES]; ring_r++; }
  portEXIT_CRITICAL(&ring_mux);
  return got;
}

static void ring_clear() {
  portENTER_CRITICAL(&ring_mux);
  ring_r = ring_w;
  portEXIT_CRITICAL(&ring_mux);
}

// ---- I2S setup: one full-duplex port, 32-bit stereo slots at 16 kHz ----
static void i2s_setup() {
  i2s_chan_config_t chan_cfg = I2S_CHANNEL_DEFAULT_CONFIG(I2S_NUM_0, I2S_ROLE);
  chan_cfg.dma_desc_num = 6;
  chan_cfg.dma_frame_num = 240;
  chan_cfg.auto_clear = true;
  ESP_ERROR_CHECK(i2s_new_channel(&chan_cfg, &tx_chan, &rx_chan));

  i2s_std_config_t std_cfg = {
    .clk_cfg  = I2S_STD_CLK_DEFAULT_CONFIG(SAMPLE_RATE),
    .slot_cfg = I2S_STD_PHILIPS_SLOT_DEFAULT_CONFIG(I2S_DATA_BIT_WIDTH_32BIT, I2S_SLOT_MODE_STEREO),
    .gpio_cfg = {
      .mclk = I2S_GPIO_UNUSED,
      .bclk = (gpio_num_t)PIN_BCLK,
      .ws   = (gpio_num_t)PIN_WS,
      .dout = (gpio_num_t)PIN_DOUT,
      .din  = (gpio_num_t)PIN_DIN,
      .invert_flags = { .mclk_inv = false, .bclk_inv = false, .ws_inv = false },
    },
  };
  ESP_ERROR_CHECK(i2s_channel_init_std_mode(tx_chan, &std_cfg));
  ESP_ERROR_CHECK(i2s_channel_init_std_mode(rx_chan, &std_cfg));
  ESP_ERROR_CHECK(i2s_channel_enable(tx_chan));
  ESP_ERROR_CHECK(i2s_channel_enable(rx_chan));
}

// ---- remote XVF3800 console: {"type":"xmos_read","id":1,"resid":35,"cmd":24,"n":1}
//                              {"type":"xmos_write","id":2,"resid":20,"cmd":12,"data":[3]}
static int json_int(const char* s, const char* key, int def) {
  char pat[24]; snprintf(pat, sizeof(pat), "\"%s\":", key);
  const char* p = strstr(s, pat);
  return p ? atoi(p + strlen(pat)) : def;
}
static void xmos_passthrough(const char* msg, bool is_write) {
  int id = json_int(msg, "id", 0), resid = json_int(msg, "resid", -1), cmd = json_int(msg, "cmd", -1);
  uint8_t buf[64]; int n = 0; bool ok = false;
  if (is_write) {
    const char* d = strstr(msg, "\"data\":[");
    if (d) { d += 8; while (*d && *d != ']' && n < 64) { buf[n++] = (uint8_t)atoi(d); while (*d && *d != ',' && *d != ']') d++; if (*d == ',') d++; } }
    ok = resid >= 0 && cmd >= 0 && xmos_write(resid, cmd, buf, n);
  } else {
    n = json_int(msg, "n", 1); if (n > 60) n = 60;
    ok = resid >= 0 && cmd >= 0 && xmos_read(resid, cmd, buf, n);
  }
  String out = String("{\"type\":\"xmos\",\"id\":") + id + ",\"ok\":" + (ok ? "true" : "false") + ",\"data\":[";
  if (!is_write && ok) for (int i = 0; i < n; i++) { if (i) out += ","; out += buf[i]; }
  out += "]}";
  ws.sendTXT(out);
}

// Boot-time scan: try each free header pin as the I2S data-in and describe what arrives.
// Real audio = many distinct values at modest level; a floating pin = stuck at 0 / -1 or glitchy full-scale.
static void din_scan() {
  struct Cand { const char* name; int gpio; };
  const Cand cand[] = { {"D6", 43}, {"D10", 9}, {"D0", 1}, {"D1", 2}, {"D2", 3}, {"D3", 4} };
  static int32_t buf[1600 * 2];
  Serial.println("[dinscan] pin  frames  stuck(0/-1)%  distinct16  min16   max16   verdict");
  for (auto& c : cand) {
    i2s_channel_disable(rx_chan);
    i2s_std_gpio_config_t g = { .mclk = I2S_GPIO_UNUSED, .bclk = (gpio_num_t)PIN_BCLK, .ws = (gpio_num_t)PIN_WS,
                                .dout = (gpio_num_t)PIN_DOUT, .din = (gpio_num_t)c.gpio,
                                .invert_flags = { .mclk_inv = false, .bclk_inv = false, .ws_inv = false } };
    i2s_channel_reconfig_std_gpio(rx_chan, &g);
    i2s_channel_enable(rx_chan);
    size_t n = 0; i2s_channel_read(rx_chan, buf, sizeof(buf), &n, 300);       // flush
    uint32_t frames = 0, stuck = 0; int16_t mn = 32767, mx = -32768; uint32_t seen[64] = {0}; int distinct = 0;
    for (int rep = 0; rep < 5; rep++) {
      if (i2s_channel_read(rx_chan, buf, sizeof(buf), &n, 300) != ESP_OK) continue;
      for (size_t i = 0; i < n / 8; i++) {
        int32_t l = buf[2 * i]; int16_t v = (int16_t)(l >> 16);
        frames++; if (l == 0 || l == -1) stuck++;
        if (v < mn) mn = v; if (v > mx) mx = v;
        uint32_t h = ((uint16_t)v * 2654435761u) >> 21;                      // 2048-bucket bitmap
        if (!(seen[h >> 5] & (1u << (h & 31)))) { seen[h >> 5] |= (1u << (h & 31)); distinct++; }
      }
    }
    int stuck_pct = frames ? (int)(100 * stuck / frames) : 100;
    const char* v = frames == 0 ? "no data" : stuck_pct > 90 ? "idle/floating" :
                    (distinct > 300 && stuck_pct < 20 && mx < 30000 && mn > -30000) ? "LOOKS LIKE AUDIO" : "glitchy/unclear";
    Serial.printf("[dinscan] %-4s %6lu  %3d           %5d      %6d  %6d  %s\n", c.name, (unsigned long)frames, stuck_pct, distinct, mn, mx, v);
  }
  i2s_channel_disable(rx_chan);
  i2s_std_gpio_config_t g = { .mclk = I2S_GPIO_UNUSED, .bclk = (gpio_num_t)PIN_BCLK, .ws = (gpio_num_t)PIN_WS,
                              .dout = (gpio_num_t)PIN_DOUT, .din = (gpio_num_t)PIN_DIN,
                              .invert_flags = { .mclk_inv = false, .bclk_inv = false, .ws_inv = false } };
  i2s_channel_reconfig_std_gpio(rx_chan, &g);
  i2s_channel_enable(rx_chan);
}

// ---- WebSocket events ----
static void on_ws_event(WStype_t type, uint8_t* payload, size_t length) {
  switch (type) {
    case WStype_CONNECTED: {
      connected = true;
      String hello = String("{\"type\":\"hello\",\"device\":\"xiao-xvf3800\",\"sr\":") + SAMPLE_RATE +
                     ",\"fw\":\"" FW_VERSION "\",\"ip\":\"" + WiFi.localIP().toString() + "\"}";
      ws.sendTXT(hello);
      Serial.println("[ws] connected");
      break;
    }
    case WStype_DISCONNECTED:
      connected = false;
      ring_clear();
      Serial.println("[ws] disconnected");
      break;
    case WStype_BIN:                                   // playback PCM
      ring_push((const int16_t*)payload, length / 2);
      tx_packets++;
      break;
    case WStype_TEXT:
      if (strstr((const char*)payload, "\"din_pull\"")) {     // diagnostic: weak pull on the I2S data-in pin
        const char* m = (const char*)payload;
        gpio_set_pull_mode((gpio_num_t)PIN_DIN, strstr(m, "\"up\"") ? GPIO_PULLUP_ONLY : strstr(m, "\"down\"") ? GPIO_PULLDOWN_ONLY : GPIO_FLOATING);
        raw_min_l = raw_max_l = raw_min_r = raw_max_r = 0;
        ws.sendTXT("{\"type\":\"ack\",\"what\":\"din_pull\"}");
      }
      else if (strstr((const char*)payload, "\"xmos_read\"")) xmos_passthrough((const char*)payload, false);
      else if (strstr((const char*)payload, "\"xmos_write\"")) xmos_passthrough((const char*)payload, true);
      else if (strstr((const char*)payload, "\"stop\"")) ring_clear();
      else if (strstr((const char*)payload, "\"ping\"")) ws.sendTXT("{\"type\":\"pong\"}");
      break;
    default:
      break;
  }
}

// ---- audio task: pump I2S both ways, independent of Wi-Fi hiccups ----
static void audio_task(void*) {
  static int32_t rx_buf[PACKET_SAMPLES * 2];           // stereo 32-bit
  static int16_t mic[PACKET_SAMPLES];
  static int32_t tx_buf[PACKET_SAMPLES * 2];
  static int16_t spk[PACKET_SAMPLES];
  size_t n;
  for (;;) {
    // 1. speaker: 20 ms from the ring (silence if empty) -> 32-bit stereo
    size_t got = ring_pop(spk, PACKET_SAMPLES);
    for (size_t i = 0; i < PACKET_SAMPLES; i++) {
      int32_t v = i < got ? ((int32_t)spk[i] << 16) : 0;
      tx_buf[2 * i] = v;
      tx_buf[2 * i + 1] = v;
    }
    i2s_channel_write(tx_chan, tx_buf, sizeof(tx_buf), &n, portMAX_DELAY);

    // 2. mic: 20 ms of stereo 32-bit -> chosen channel -> int16 -> queue for loop()
    if (i2s_channel_read(rx_chan, rx_buf, sizeof(rx_buf), &n, portMAX_DELAY) == ESP_OK) {
      size_t frames = n / 8;
      bool all_zero = true;
      MicPacket pkt; pkt.n = frames;
      for (size_t i = 0; i < frames; i++) {
        int32_t l = rx_buf[2 * i], r = rx_buf[2 * i + 1];
        if (l < raw_min_l) raw_min_l = l; if (l > raw_max_l) raw_max_l = l;
        if (r < raw_min_r) raw_min_r = r; if (r > raw_max_r) raw_max_r = r;
        if (l | r) all_zero = false;
        pkt.s[i] = (int16_t)(rx_buf[2 * i + MIC_CHANNEL] >> 16);
      }
      raw_frames += frames; if (all_zero) raw_zero_frames += frames;
      if (connected && xQueueSend(mic_q, &pkt, 0) != pdTRUE) mic_dropped++;
    }
  }
}

// Boot-time electrical self-test of the header pins that go to the reSpeaker board.
// For each pin: does it follow the ESP32's weak pull-up/pull-down (nothing strong attached),
// or is it held HIGH / LOW by the board? For each neighbouring pair: are they shorted?
struct HdrPin { const char* name; int gpio; };
static const HdrPin HDR[] = { {"D3", 4}, {"D4/SDA", 5}, {"D5/SCL", 6}, {"D6/mic-in", 43},
                              {"D10", 9}, {"D9/BCLK", 8}, {"D8/WS", 7}, {"D7/spk-out", 44} };
static void pin_selftest() {
  Serial.println("[selftest] pin        pull-up pull-down  verdict");
  for (auto& h : HDR) {
    pinMode(h.gpio, INPUT_PULLUP);   delay(3); int up = digitalRead(h.gpio);
    pinMode(h.gpio, INPUT_PULLDOWN); delay(3); int dn = digitalRead(h.gpio);
    pinMode(h.gpio, INPUT);
    const char* v = (up && !dn) ? "floating (no strong connection)" : (up && dn) ? "held HIGH by board" : (!up && !dn) ? "held LOW (short to GND or driven low)" : "odd";
    Serial.printf("[selftest] %-11s %d       %d          %s\n", h.name, up, dn, v);
  }
  // neighbours on the left column (D3..D6) and right column (D10..D7)
  const int pairs[][2] = { {0, 1}, {1, 2}, {2, 3}, {4, 5}, {5, 6}, {6, 7} };
  for (auto& pr : pairs) {
    const HdrPin& a = HDR[pr[0]]; const HdrPin& b = HDR[pr[1]];
    if (a.gpio == 43) continue;                         // never drive the board's output pin
    pinMode(b.gpio, INPUT_PULLUP);
    pinMode(a.gpio, INPUT_PULLUP); delay(3); int rel = digitalRead(b.gpio);
    pinMode(a.gpio, OUTPUT); digitalWrite(a.gpio, LOW); delay(3); int low = digitalRead(b.gpio);
    pinMode(a.gpio, INPUT); pinMode(b.gpio, INPUT);
    Serial.printf("[selftest] %s <-> %s : %s\n", a.name, b.name,
                  (rel == 1 && low == 0) ? "SHORTED TOGETHER" : "not shorted");
  }
}

void setup() {
  Serial.begin(115200);
  delay(300);
  Serial.printf("\nSmitty XIAO bridge %s\n", FW_VERSION);
  delay(1500);                                         // let the USB serial attach so the self-test is visible
  pin_selftest();

  WiFi.mode(WIFI_STA);
  WiFi.setSleep(false);                                // low latency matters more than power
  WiFi.begin(WIFI_SSID, WIFI_PASSWORD);
  Serial.print("[wifi] connecting to " WIFI_SSID);
  uint32_t t0 = millis();
  while (WiFi.status() != WL_CONNECTED) {
    delay(500); Serial.print(".");
    if (millis() - t0 > 15000) {                       // not joining: show what this 2.4 GHz radio can see
      Serial.println("\n[wifi] still not connected; networks visible to the ESP32 (2.4 GHz only):");
      WiFi.disconnect(true, false);
      delay(200);
      int n = WiFi.scanNetworks();
      for (int i = 0; i < n; i++)
        Serial.printf("   %-32s ch %2d  %4d dBm  %s\n", WiFi.SSID(i).c_str(), WiFi.channel(i), WiFi.RSSI(i),
                      WiFi.encryptionType(i) == WIFI_AUTH_OPEN ? "open" : "secured");
      if (n == 0) Serial.println("   (none)");
      WiFi.begin(WIFI_SSID, WIFI_PASSWORD);
      t0 = millis();
    }
  }
  Serial.printf("\n[wifi] %s  rssi %d dBm\n", WiFi.localIP().toString().c_str(), WiFi.RSSI());

  Wire.begin();                                        // XIAO default SDA/SCL (D4/D5)
  Wire.setClock(100000);
  Serial.printf("[i2c] SDA=%d SCL=%d  devices: %s\n", SDA, SCL, i2c_scan().c_str());
  xmos_report();
  i2s_setup();
  Serial.printf("[i2s] %d Hz, 32-bit stereo, role %s\n", SAMPLE_RATE, I2S_ROLE == I2S_ROLE_MASTER ? "master" : "slave");
  delay(1500);                                         // give the XVF3800 time to lock onto the clocks
  xmos_report();
  din_scan();

  // Over-the-air updates: `arduino-cli upload -p smitty-xiao.local --protocol network ...`
  // (ArduinoOTA also starts mDNS, so the board answers to smitty-xiao.local)
  ArduinoOTA.setHostname("smitty-xiao");
  ArduinoOTA.onStart([]() { Serial.println("[ota] start"); if (rx_chan) i2s_channel_disable(rx_chan); if (tx_chan) i2s_channel_disable(tx_chan); });
  ArduinoOTA.onEnd([]() { Serial.println("[ota] done, rebooting"); });
  ArduinoOTA.onError([](ota_error_t e) { Serial.printf("[ota] error %u\n", e); });
  ArduinoOTA.begin();
  server_ip = resolve_server();
  ws.begin(server_ip.c_str(), SERVER_PORT, SERVER_PATH);
  ws.onEvent(on_ws_event);
  ws.setReconnectInterval(2000);
  ws.enableHeartbeat(5000, 3000, 2);

  mic_q = xQueueCreate(16, sizeof(MicPacket));
  xTaskCreatePinnedToCore(audio_task, "audio", 8192, nullptr, 5, nullptr, 1);
}

// Watch the codec (0x18) and the XVF3800 (0x2C) twice a second; print every change.
// Shows the XVF3800 appearing/disappearing as power or firmware state changes.
static void presence_watch() {
  static uint32_t last = 0; static int prev = -1;
  if (millis() - last < 500) return;
  last = millis();
  Wire.beginTransmission(0x18); bool codec = Wire.endTransmission() == 0;
  Wire.beginTransmission(0x2C); bool xvf = Wire.endTransmission() == 0;
  int now = (codec ? 1 : 0) | (xvf ? 2 : 0);
  if (now != prev) {
    prev = now;
    Serial.printf("[presence] t=%.1fs codec(0x18)=%s xvf3800(0x2C)=%s\n", millis() / 1000.0,
                  codec ? "YES" : "no", xvf ? "YES" : "no");
    if (connected) {
      char js[120];
      snprintf(js, sizeof(js), "{\"type\":\"presence\",\"t\":%.1f,\"codec\":%s,\"xvf3800\":%s}",
               millis() / 1000.0, codec ? "true" : "false", xvf ? "true" : "false");
      ws.sendTXT(js);
    }
  }
}

void loop() {
  ArduinoOTA.handle();
  presence_watch();
  ws.loop();
  MicPacket pkt;
  while (xQueueReceive(mic_q, &pkt, 0) == pdTRUE) {      // drain mic packets to Wi-Fi
    if (connected) { ws.sendBIN((uint8_t*)pkt.s, pkt.n * 2); rx_packets++; }
  }
  static uint32_t last = 0;
  if (millis() - last > 5000) {
    last = millis();
    Serial.printf("[stat] ws=%s rx(mic)=%lu tx(spk)=%lu ring=%lu drop=%lu rssi=%d ip=%s\n",
                  connected ? "up" : "down", (unsigned long)rx_packets, (unsigned long)tx_packets,
                  (unsigned long)ring_count(), (unsigned long)mic_dropped, WiFi.RSSI(),
                  WiFi.localIP().toString().c_str());
    Serial.printf("[i2s] raw L %ld..%ld  R %ld..%ld  frames=%lu zero=%lu\n",
                  (long)raw_min_l, (long)raw_max_l, (long)raw_min_r, (long)raw_max_r,
                  (unsigned long)raw_frames, (unsigned long)raw_zero_frames);
    xmos_report();
    if (!x_ok) Serial.printf("[i2c] devices: %s\n", i2c_scan().c_str());
    if (connected) {                                   // same diagnostics to the server (no serial needed)
      char js[360];
      snprintf(js, sizeof(js),
        "{\"type\":\"stat\",\"fw\":\"" FW_VERSION "\",\"role\":\"%s\",\"rssi\":%d,\"mic_pkts\":%lu,\"spk_pkts\":%lu,"
        "\"dropped\":%lu,\"i2s_frames\":%lu,\"raw_l\":[%ld,%ld],\"raw_r\":[%ld,%ld],"
        "\"xmos_ok\":%s,\"xmos_version\":\"%u.%u.%u\",\"i2s_inactive\":%u,\"mute\":%u,\"amp_en_low_is_on\":%u}",
        I2S_ROLE == I2S_ROLE_MASTER ? "master" : "slave", WiFi.RSSI(), (unsigned long)rx_packets, (unsigned long)tx_packets,
        (unsigned long)mic_dropped, (unsigned long)raw_frames, (long)raw_min_l, (long)raw_max_l, (long)raw_min_r, (long)raw_max_r,
        x_ok ? "true" : "false", x_ver[0], x_ver[1], x_ver[2], x_inactive, x_gpo[1], x_gpo[2]);
      ws.sendTXT(js);
    }
    raw_min_l = raw_max_l = raw_min_r = raw_max_r = 0; raw_frames = raw_zero_frames = 0;
    static uint32_t down_since = 0;
    if (connected) down_since = 0;
    else {
      if (!down_since) down_since = millis();
      // Down for a while: the Mac may have a new IP. One quick mDNS query; never block the loop.
      if (millis() - down_since > 15000 && String(SERVER_HOST).endsWith(".local")) {
        down_since = millis();
        String fresh = resolve_server(1);
        if (fresh != server_ip) { server_ip = fresh; ws.begin(server_ip.c_str(), SERVER_PORT, SERVER_PATH); }
      }
    }
  }
  delay(1);
}
