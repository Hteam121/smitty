import AVFoundation
import Observation
import Speech

/// Live speech-to-text for the chat composer: tap the mic, talk, the words appear in the field as
/// you go, tap stop (or the field) and edit or send. Smitty only ever receives text, so this is
/// transcription on the phone rather than an audio message.
@MainActor @Observable
final class Dictation {
    enum State: Equatable { case idle, starting, listening, denied(String) }

    private(set) var state: State = .idle
    private(set) var transcript = ""
    private(set) var level: Float = 0          // mic loudness 0…1, for the pulse

    var isListening: Bool { state == .listening || state == .starting }
    var problem: String? { if case .denied(let why) = state { return why }; return nil }

    // Created on first use: SwiftUI builds a fresh `@State` initial value on every view init.
    private var engine: AVAudioEngine?
    private let recognizer = SFSpeechRecognizer()
    private var request: SFSpeechAudioBufferRecognitionRequest?
    private var task: SFSpeechRecognitionTask?

    func start() async {
        guard !isListening else { return }
        state = .starting
        transcript = ""
        guard await AVAudioApplication.requestRecordPermission() else {
            state = .denied("Microphone access is off for Smitty (Settings → Smitty)."); return
        }
        let auth = await withCheckedContinuation { c in SFSpeechRecognizer.requestAuthorization { c.resume(returning: $0) } }
        guard auth == .authorized else {
            state = .denied("Speech recognition is off for Smitty (Settings → Smitty)."); return
        }
        guard let recognizer, recognizer.isAvailable else {
            state = .denied("Speech recognition isn't available right now."); return
        }
        do {
            let session = AVAudioSession.sharedInstance()
            try session.setCategory(.record, mode: .measurement, options: .duckOthers)
            try session.setActive(true, options: .notifyOthersOnDeactivation)
            let req = SFSpeechAudioBufferRecognitionRequest()
            req.shouldReportPartialResults = true
            request = req
            let engine = self.engine ?? AVAudioEngine()
            self.engine = engine
            let input = engine.inputNode
            input.removeTap(onBus: 0)
            input.installTap(onBus: 0, bufferSize: 1024, format: input.outputFormat(forBus: 0)) { [weak self] buf, _ in
                req.append(buf)
                guard let ch = buf.floatChannelData?[0] else { return }
                let n = Int(buf.frameLength)
                var sum: Float = 0
                for i in 0..<n { sum += ch[i] * ch[i] }
                let rms = (sum / Float(max(n, 1))).squareRoot()
                Task { @MainActor in self?.level = min(1, rms * 6) }
            }
            engine.prepare()
            try engine.start()
            state = .listening
            task = recognizer.recognitionTask(with: req) { [weak self] result, error in
                Task { @MainActor in
                    guard let self else { return }
                    if let result { self.transcript = result.bestTranscription.formattedString }
                    if error != nil || result?.isFinal == true { self.teardown() }
                }
            }
        } catch {
            teardown()
            state = .denied("Could not start the microphone: \(error.localizedDescription)")
        }
    }

    /// Stop listening; the last partial transcript stays, and a final (more accurate) one may still arrive.
    func stop() {
        guard isListening else { return }
        engine?.stop()
        engine?.inputNode.removeTap(onBus: 0)
        request?.endAudio()
        task?.finish()
        level = 0
        state = .idle
        try? AVAudioSession.sharedInstance().setActive(false, options: .notifyOthersOnDeactivation)
    }

    private func teardown() {
        if let engine, engine.isRunning { engine.stop(); engine.inputNode.removeTap(onBus: 0) }
        request = nil
        task = nil
        level = 0
        if state == .listening || state == .starting { state = .idle }
    }
}
