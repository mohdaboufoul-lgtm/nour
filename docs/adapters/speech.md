# Speech adapters: Arabic speech-to-text and text-to-speech

Reference for the engineers building the Arabic speech pipeline of SPEC section 9, the week-one bake-off of section 16 and the voice rule of section 3. Facts below were checked on 2026-10-02 against the linked vendor pages; anything we could not confirm from an official page is marked **unverified**. Prices are list prices in USD and change without notice; re-check before budgeting (the AI-model budget line inside the AED 3,000 cap is still open, section 17).

Companion tool: `tools/stt_bakeoff.py` scores engine outputs (WER, CER, custom-vocabulary recall) and ranks engines; tests in `tests/tools/test_stt_bakeoff.py`.

## 1. What the spec requires

| Requirement | Source | What it means for the adapter |
| --- | --- | --- |
| "a dialect-capable Arabic speech model. Selection by test, not reputation: 50 real owner voice notes, two or three engines (Azure Speech Levantine and Gulf locales, Google, ElevenLabs Scribe, Whisper as baseline), pick the lowest word-error rate on the owner's voice. Re-test quarterly." | SPEC 9 | The engine is a configuration choice behind one port; the bake-off is a repeatable script, not a one-off. |
| "Custom vocabulary loaded into the recogniser: company names, product names, staff and customer names, place names, the owner's recurring phrases." | SPEC 9 | Every candidate must accept a phrase list / keyterms / adaptation set at request time, or lose points on the vocabulary-recall score. |
| "Code-switching and Arabizi ("3ala", "sho el 2akhbar") parsed as Arabic." | SPEC 9 | Mixed Arabic/English speech must not be dropped; Arabizi is a typed-text phenomenon handled after STT (section 5 below). |
| Read-back rule: one text line of what she understood, wait for yes, for money, sends in the owner's name, record changes. | SPEC 9 | The port must return a confidence so the agent can also read back when recognition is shaky. |
| "Synthesis: one locked Lebanese-accented voice" ... "One licensed or synthetic Arabic voice with a Lebanese accent, chosen once and locked like the face. Never a clone of a real person." | SPEC 3, 9 | TTS port enforces a voice lock; no instant/professional voice cloning of anyone, ever. |
| AI voice line: "transcripts kept, audio deleted after 7 days" (`config/channels.yaml: audio_retention_days: 7`). | SPEC 9 | Audio is a 7-day object; the transcript and its hash are what the audit log keeps. |
| Hosting in a UAE region; Tier 2 content only on a private in-region model. | SPEC 4, 17 | Data residency per engine matters; anything Tier 2 never goes to an out-of-region speech API. |
| Week one: "Arabic speech bake-off on 50 owner voice notes; custom vocabulary loaded; read-back implemented"; owner input: "50 real voice notes for the Arabic speech bake-off". | SPEC 16, 17 | Phase 0 cannot close without the 50 notes and the bake-off report. |

## 2. The input: WhatsApp voice notes

- Inbound voice notes arrive through the WhatsApp Cloud API webhook as `type: "audio"` with `audio.mime_type: "audio/ogg; codecs=opus"`, `audio.id`, `audio.sha256` and `audio.voice: true` ("Boolean indicating if audio is a recording made with the WhatsApp client voice recording feature"). Source: [Meta, audio message webhook reference](https://developers.facebook.com/documentation/business-messaging/whatsapp/webhooks/reference/messages/audio/).
- To fetch the bytes: `GET /{media-id}` returns a download URL that expires after 5 minutes; download it with the bearer token. Source: [Meta, Cloud API media reference](https://developers.facebook.com/docs/whatsapp/cloud-api/reference/media).
- Outbound audio accepted by the Cloud API: AAC, AMR, MP3, MP4 audio, and OGG with "OPUS codecs only; base audio/ogg not supported; mono input only", 16 MB maximum. Sending OGG/Opus is what makes the client show the microphone (voice-note) icon. Same source, and [Meta, audio messages](https://developers.facebook.com/documentation/business-messaging/whatsapp/messages/audio-messages).
- Consequence: the STT adapter must take OGG/Opus mono directly or transcode it (ffmpeg to 16 kHz 16-bit mono PCM WAV is the universal fallback every engine accepts), and the TTS adapter must emit OGG/Opus mono for voice-note replies.

## 3. Candidate recognisers at a glance

| | Azure AI Speech | Google Cloud Speech-to-Text v2 | ElevenLabs Scribe v2 | Whisper large-v3 (self-hosted) / OpenAI audio API |
| --- | --- | --- | --- | --- |
| Arabic locales | 18 `ar-*` locales incl. `ar-LB`, `ar-SY`, `ar-JO`, `ar-AE`, `ar-SA`, `ar-EG` | 19 `ar-*` codes incl. `ar-LB`, `ar-AE`, `ar-SA`, `ar-EG`, plus `ar-XA`; Arabic on `chirp_3` is **Preview** | one language code `ar`/`ara`, dialect not selectable | one language `ar`, dialect not selectable |
| Custom vocabulary | Phrase list (SDK and fast transcription), up to 2,000 phrases; custom speech training | Model adaptation phrase sets with boost (up to 1,000 phrases on `chirp_3`) | `keyterms`: up to 1,000 terms (batch), 50 (realtime); +$0.05/h | `prompt` / `initial_prompt` text hint only; no boosting |
| OGG/Opus input | Yes (SDK via GStreamer; REST short audio; fast and batch transcription) | Yes (`OGG_OPUS` explicit decoding) | Yes (`audio/opus`, `audio/webm`) | Self-hosted: anything ffmpeg decodes. OpenAI API guide lists mp3, mp4, mpeg, mpga, m4a, wav, webm (ogg not listed) |
| Streaming | Yes, SDK real-time with partial results | Yes, StreamingRecognize (about 5 min sessions) | Yes, `scribe_v2_realtime` WebSocket (~150 ms) | whisper-1: no. gpt-4o-transcribe family: streaming and realtime |
| Price basis | per audio hour: $1.00 real-time, $0.18 batch; 5 free hours/month (F0) | per minute: $0.016 (first 500k min/month), dynamic batch $0.003; 60 free min/month | per audio hour: $0.22 batch, $0.39 realtime | self-hosted: GPU cost only. API: gpt-4o-transcribe $0.006/min, mini $0.003/min |
| UAE residency | Yes: `uaenorth` has real-time and batch STT (not fast transcription, not custom-speech training) | No: `chirp_3` only in `us` and `eu` multi-regions | No: US default; EU, India, Singapore enterprise environments | Self-hosted in the UAE server: full control. OpenAI API: UAE storage, but audio processing only in US/Europe |
| Response gives | text, per-phrase confidence, word offsets/durations, N-best (SDK/short REST) | transcript, confidence, word timings and word confidence (`long`/`short`) | text, `language_probability`, words with `start`/`end`/`logprob`/`speaker_id` | text, segments; word timestamps (whisper-1 / self-hosted), token logprobs (gpt-4o-transcribe) |

## 4. Engine details

### 4.1 Azure AI Speech (Microsoft)

**Arabic locales for speech to text.** `ar-AE`, `ar-BH`, `ar-DZ`, `ar-EG`, `ar-IL`, `ar-IQ`, `ar-JO`, `ar-KW`, `ar-LB`, `ar-LY`, `ar-MA`, `ar-OM`, `ar-PS`, `ar-QA`, `ar-SA`, `ar-SY`, `ar-TN`, `ar-YE`. All 18 support real-time, batch and fast transcription and custom speech training with audio + human-labelled transcripts and plain text. `ar-EG` and `ar-SA` also accept structured text; `ar-SA` is the only Arabic locale with "Output format, Phrase list" listed in the custom-speech column and the only one with post-stream refinement. Source: [Language and voice support (STT tab)](https://learn.microsoft.com/en-us/azure/ai-services/speech-service/language-support?tabs=stt). Levantine candidates for the owner: `ar-LB` first, then `ar-SY`, `ar-JO`, `ar-PS`. Gulf candidates for the UAE environment: `ar-AE`, `ar-SA`. The spec asks for both families in the bake-off.

**Custom vocabulary.** Phrase list is a runtime feature: "You provide a phrase list just before starting the speech recognition, so you don't need to train a custom model", "a phrase list shouldn't have more than 2,000 phrases", weight `0.0` to `2.0` (default `1.0`). It works with real-time transcription (Speech SDK, CLI, Studio), the fast transcription API, LLM speech and Voice Live; "The Batch transcription API doesn't support phrase lists." Source: [Improve recognition accuracy with phrase list](https://learn.microsoft.com/en-us/azure/ai-services/speech-service/improve-accuracy-phrase-list). **Unverified:** the phrase-list how-to does not restrict languages, but the locale table flags "Phrase list" only under `ar-SA`; measure the phrase-list effect on `ar-LB` in the bake-off rather than assume it. For lists larger than 2,000 terms or persistent gains, train a custom speech model with plain-text data; training needs a region with dedicated hardware (not `uaenorth`); the model can then be copied to `uaenorth`. Source: [Supported regions](https://learn.microsoft.com/en-us/azure/ai-services/speech-service/regions).

SDK (Python):

```python
import azure.cognitiveservices.speech as speechsdk

cfg = speechsdk.SpeechConfig(subscription=key, region="uaenorth")
cfg.speech_recognition_language = "ar-LB"
fmt = speechsdk.audio.AudioStreamFormat(
    compressed_stream_format=speechsdk.AudioStreamContainerFormat.OGG_OPUS)
stream = speechsdk.audio.PushAudioInputStream(stream_format=fmt)   # needs GStreamer on the host
reco = speechsdk.SpeechRecognizer(speech_config=cfg,
                                  audio_config=speechsdk.audio.AudioConfig(stream=stream))
phrases = speechsdk.PhraseListGrammar.from_recognizer(reco)
for term in vocabulary:            # company, product, staff, customer and place names
    phrases.addPhrase(term)
```

Fast transcription (REST, not available in `uaenorth`, see residency):

```text
POST https://{resource}.cognitiveservices.azure.com/speechtotext/transcriptions:transcribe?api-version=2025-10-15
Ocp-Apim-Subscription-Key: ...
Content-Type: multipart/form-data
  audio=@note.ogg
  definition={"locales":["ar-LB"],
              "phraseList":{"phrases":["Buzz Avenue","نور"],"biasing_weight":1.5},
              "profanityFilterMode":"None"}
```

Response: `durationMilliseconds`, `combinedPhrases[].{channel,text}`, `phrases[].{offsetMilliseconds,durationMilliseconds,text,words[].{text,offsetMilliseconds,durationMilliseconds},locale,confidence,channel,speaker}`. Display form only (punctuated). `locales: []` enables automatic multilingual detection. Formats: WAV, MP3, OPUS/OGG, FLAC, WMA, AAC, ALAW/MULAW in WAV, AMR, WebM, SPEEX; up to 500 MB and 5 hours. Source: [Fast transcription](https://learn.microsoft.com/en-us/azure/ai-services/speech-service/fast-transcription-create).

Short-audio REST (any region, max 60 s of audio, final results only): `POST https://{resource}.cognitiveservices.azure.com/stt/speech/recognition/conversation/cognitiveservices/v1?language=ar-LB&format=detailed` with `Content-Type: audio/ogg; codecs=opus` (16 kHz mono) or `audio/wav; codecs=audio/pcm; samplerate=16000`. Response: `RecognitionStatus` (`Success`, `NoMatch`, `InitialSilenceTimeout`, `BabbleTimeout`, `Error`), `DisplayText`, `Offset` and `Duration` in 100-nanosecond ticks, and with `format=detailed` an `NBest[]` of `{Confidence, Lexical, ITN, MaskedITN, Display}`. Source: [REST API for short audio](https://learn.microsoft.com/en-us/azure/ai-services/speech-service/rest-speech-to-text-short). Voice notes longer than 60 s must go through the SDK or batch.

**Audio formats.** SDK and CLI decode MP3, OPUS/OGG, FLAC, ALAW, MULAW and "ANY" through GStreamer ("GStreamer binaries aren't compiled and linked with the Speech SDK"; on Debian/Ubuntu install `libgstreamer1.0-0 gstreamer1.0-plugins-base/good/bad/ugly`); the JavaScript SDK does not support compressed input. Source: [How to use compressed input audio](https://learn.microsoft.com/en-us/azure/ai-services/speech-service/how-to-use-codec-compressed-audio-input-streams). Batch transcription accepts the same list as fast transcription, files up to 1 GB, supplied as public URIs, SAS URLs or a blob container with managed identity. Sources: [Batch transcription audio data](https://learn.microsoft.com/en-us/azure/ai-services/speech-service/batch-transcription-audio-data), [Quotas and limits](https://learn.microsoft.com/en-us/azure/ai-services/speech-service/speech-services-quotas-and-limits).

**Streaming vs batch.** Real-time SDK recognition gives partial (`recognizing`) and final (`recognized`) events; 100 concurrent requests per S0 resource by default. Batch is asynchronous (jobs, blob storage). Fast transcription is synchronous REST for files. Language identification: candidate list via `AutoDetectSourceLanguageConfig`, "up to four languages for at-start LID or up to 10 languages for continuous LID"; "Continuous LID doesn't support changing languages within the same sentence". Source: [Language identification](https://learn.microsoft.com/en-us/azure/ai-services/speech-service/language-identification).

**Pricing basis.** Per audio hour for STT, per million characters for TTS. Azure Retail Prices API, region `uaenorth`, queried 2026-10-02: `S1 Speech To Text` $1.00 per hour; `S1 Neural Text To Speech Characters` $15.00 per 1M; `S1 Custom Text To Speech Characters` $6.00 per 1M; `S1 Speech Translation` $2.50 per hour. The batch meter `S1 Speech to Text Batch` is $0.18 per hour in `eastus`; the `uaenorth` listing returned no batch meter (**unverified** whether batch is billed at the same rate there). No fast-transcription meter was returned by the API; see the pricing page. Free tier (F0): "5 audio hours free per month" for STT and "0.5 million characters free per month" for TTS. Sources: [Azure Retail Prices API](https://prices.azure.com/api/retail/prices) (`$filter=armRegionName eq 'uaenorth' and contains(productName,'Speech')`), [Speech pricing page](https://azure.microsoft.com/en-us/pricing/details/cognitive-services/speech-services/) (prices render client-side; the page itself lists the free allowances).

**Data residency.** "Azure Speech doesn't store or process your data outside the region of your Azure Speech resource." `uaenorth` supports real-time transcription and batch transcription but **not** fast transcription, Whisper via batch, custom speech training or post-stream refinement; for TTS, `uaenorth` supports neural text to speech, batch synthesis and custom voice hosting but not HD voices or personal voice. `qatarcentral` is similar. Source: [Supported regions](https://learn.microsoft.com/en-us/azure/ai-services/speech-service/regions). (A 2021 Q&A answer saying UAE was unsupported is superseded by this page.) Practical consequence: in `uaenorth`, custom vocabulary means the SDK phrase list on real-time recognition, because fast transcription is absent and batch ignores phrase lists.

### 4.2 Google Cloud Speech-to-Text v2

**Arabic codes.** `ar-AE`, `ar-BH`, `ar-DZ`, `ar-EG`, `ar-IL`, `ar-IQ`, `ar-JO`, `ar-KW`, `ar-LB`, `ar-MA`, `ar-MR`, `ar-OM`, `ar-PS`, `ar-QA`, `ar-SA`, `ar-SY`, `ar-TN`, `ar-YE` and `ar-XA` (generic Arabic). Models per code: `chirp_3`, `long`, `short` for most; `ar-SY` and `ar-XA` on `chirp_3` only. Features listed: automatic punctuation, model adaptation, profanity filter, and word-level confidence on `long`/`short`. There are no `ar-x-gulf` / `ar-x-levant` codes. All Arabic variants on `chirp_3` are in **Preview**. Sources: [Supported languages](https://docs.cloud.google.com/speech-to-text/v2/docs/speech-to-text-supported-languages), [Chirp 3](https://docs.cloud.google.com/speech-to-text/docs/models/chirp-3).

**Custom vocabulary (model adaptation).** Inline phrase sets in the request: `adaptation.phrase_sets[].inline_phrase_set.phrases[].{value, boost}` or a reusable `PhraseSet` resource (`adaptation.phrase_sets[].phrase_set`). Boost is "a float value greater than 0"; "practical maximum limit for boost values is 20". Chirp 3 supports adaptation "up to 1,000 phrases". Content limits: 5,000 phrases and 100,000 characters per request, 100 characters per phrase. Sources: [Model adaptation](https://docs.cloud.google.com/speech-to-text/v2/docs/adaptation-model), [Chirp 3](https://docs.cloud.google.com/speech-to-text/docs/models/chirp-3), [Quotas and limits](https://docs.cloud.google.com/speech-to-text/quotas).

**Audio formats.** `OGG_OPUS` and `WEBM_OPUS` (8, 12, 16, 24 or 48 kHz), `FLAC`, `LINEAR16`, `MULAW`, `AMR` (8 kHz), `AMR_WB` (16 kHz), `SPEEX_WITH_HEADER_BYTE`; MP3 only in `v1p1beta1`. Use `explicit_decoding_config` for Opus (**unverified** whether `auto_decoding_config` sniffs Ogg containers reliably). Source: [Audio encodings](https://docs.cloud.google.com/speech-to-text/v2/docs/encoding). Limits: synchronous `recognize` about 1 minute and 10 MB inline; streaming sessions about 5 minutes; `BatchRecognize` up to about 480 minutes from Cloud Storage (`chirp_3` batch word-level timestamps up to 20 minutes). Sources: [Quotas and limits](https://docs.cloud.google.com/speech-to-text/quotas), [Chirp 3](https://docs.cloud.google.com/speech-to-text/docs/models/chirp-3).

**Request/response.** `POST .../v2/projects/{project}/locations/{location}/recognizers/_:recognize` with `config: {model, language_codes[], auto_decoding_config | explicit_decoding_config, features: {enable_word_time_offsets, enable_word_confidence, enable_automatic_punctuation}, adaptation}` and `content` (base64) or `uri` (`gs://`). Response: `results[].alternatives[].{transcript, confidence, words[].{start_offset, end_offset, word, confidence, speaker_label}}`, `results[].language_code`, `result_end_offset`, `metadata.total_billed_duration`. Source: [recognize REST reference](https://docs.cloud.google.com/speech-to-text/docs/reference/rest/v2/projects.locations.recognizers/recognize). Chirp 3 caveat from Google: word-level confidence "isn't truly a confidence score", so treat it as ordinal only.

**Pricing basis.** Per minute, tiered per month: V2 Recognition $0.016/min for 0 to 500,000 minutes, then $0.010, $0.008, $0.004; Dynamic Batch Recognition $0.003/min; first 60 minutes/month free (V1 rows show $0.016 with data logging, $0.024 without). Source: [Speech-to-Text pricing](https://cloud.google.com/speech-to-text/pricing) (table text extracted from the page). Chirp models are billed in the "Standard" category per Google's V2 launch blog (**secondary source**).

**Data residency.** `chirp_3` is served only from the `us` and `eu` multi-regions; the V1 regional endpoints are `eu-speech.googleapis.com` and `us-speech.googleapis.com` ("your data at-rest and in-use will stay within the continental boundaries of Europe or the USA"). No Middle East location is offered for Speech-to-Text. Sources: [Chirp 3](https://docs.cloud.google.com/speech-to-text/docs/models/chirp-3), [Regional endpoints](https://docs.cloud.google.com/speech-to-text/docs/endpoints). This conflicts with the section 17 recommendation of a UAE region; Google can take part in the bake-off (owner consent to send his own notes to the EU endpoint), but cannot be the production engine for anything beyond Tier 0 unless the owner accepts EU processing.

### 4.3 ElevenLabs Scribe

**Models and Arabic.** `scribe_v2` (batch), `scribe_v2_realtime` ("~150 ms" latency), `scribe_v2_medical`. "90+ languages"; Arabic (`ara`) sits in the vendor's "Good (>10% to <=20% WER)" accuracy tier. One Arabic code; no Levantine or Gulf selection. Source: [Speech to text capability](https://elevenlabs.io/docs/capabilities/speech-to-text). ElevenLabs' own launch numbers for Arabic (FLEURS 11.1% WER, Common Voice 24.5%) are vendor benchmarks on MSA-leaning corpora, not evidence for Lebanese dialect. Source: [Introducing Scribe v2](https://elevenlabs.io/blog/introducing-scribe-v2).

**Custom vocabulary.** `keyterms`: list of strings, "Max 1,000 terms; <50 chars each; 5 words max" in batch; realtime "up to 50 keyterms (20 characters each)"; keyterm prompting is surcharged (+$0.05 per hour on the API pricing page; the API reference says +20%). Sources: [Speech-to-text convert API](https://elevenlabs.io/docs/api-reference/speech-to-text/convert), [API pricing](https://elevenlabs.io/pricing/api).

**Audio formats.** `audio/mpeg`, `audio/wav`, `audio/flac`, `audio/webm`, `audio/opus`, plus `video/mp4`, `video/quicktime`, `video/webm`. The capabilities page says files up to 3 GB and 10 hours; the API reference says `file` under 5 GB and `source_url` under 2 GB, minimum 100 ms (**inconsistent across the two official pages**; both are far above a voice note). Realtime takes PCM (8 to 48 kHz) or mu-law, so Opus must be decoded first.

**Request/response.** `POST /v1/speech-to-text` (multipart): `model_id` (required), one of `file` / `source_url`, `language_code` (ISO-639-1 or -3, so `ar` or `ara`), `tag_audio_events`, `num_speakers` (1 to 32), `timestamps_granularity` (`word` | `character`), `diarize`, `keyterms[]`, `temperature` (0.0 to 2.0), `seed` (for reproducibility), `webhook`, `use_multi_channel`. Response: `language_code`, `language_probability` (0 to 1), `text`, `words[].{text, type: word|spacing|audio_event, start, end, speaker_id, logprob, channel_index}`, `audio_duration_secs`. Source: [convert API reference](https://elevenlabs.io/docs/api-reference/speech-to-text/convert). Realtime: WebSocket `wss://api.elevenlabs.io/v1/speech-to-text/realtime`, model `scribe_v2_realtime`, audio as base64 chunks, VAD or manual `commit`, events for partial transcripts, committed transcripts (optionally with word timestamps) and errors; browser clients use a server-minted single-use token (15 minute expiry). Source: [Realtime speech to text cookbook](https://elevenlabs.io/docs/developers/guides/cookbooks/speech-to-text/streaming).

**Pricing basis.** Per audio hour: "Scribe v2 & Scribe v2 Medical" $0.22/hour on every plan, "Scribe v2 Realtime" $0.39/hour, entity detection +$0.07/hour, keyterms +$0.05/hour; plans from Free to Business ($990/month) include hours. Source: [API pricing](https://elevenlabs.io/pricing/api).

**Data residency.** "Data residency is an Enterprise feature"; default hosting is the US with isolated environments for the EU (`api.eu.residency.elevenlabs.io`), India (`api.in.residency.elevenlabs.io`) and Singapore (`api.sg.residency.elevenlabs.io`). No Middle East environment. Source: [Data residency](https://elevenlabs.io/docs/overview/administration/data-residency). Zero-retention mode is described as an enterprise option (**unverified** detail: only seen in launch material).

### 4.4 Whisper (baseline) and the OpenAI audio API

**Model.** `large-v3` (1,550M parameters, about 10 GB VRAM, 1x speed) and `turbo` (809M, about 6 GB, about 8x); "trained on 1 million hours of weakly labeled audio and 4 million hours of pseudo-labeled audio", "10% to 20% reduction of errors compared to Whisper large-v2", 99 languages; `"ar": "arabic"` is in `whisper/tokenizer.py`. Licence: the GitHub README says MIT, the Hugging Face model card says Apache-2.0 (both official OpenAI sources; **discrepancy noted**). Sources: [openai/whisper README](https://github.com/openai/whisper), [Hugging Face model card](https://huggingface.co/openai/whisper-large-v3), [tokenizer.py](https://raw.githubusercontent.com/openai/whisper/main/whisper/tokenizer.py).

**Independent Arabic numbers.** Open Universal Arabic ASR Leaderboard (Wang, Alhmoud, Alqurishi, [arXiv:2412.13788](https://arxiv.org/html/2412.13788)), average over SADA, Common Voice 18.0, MASC clean, MASC noisy and MGB-2: `openai/whisper-large-v3` 29.87% WER / 13.65% CER (SADA 44.52, CV-18 8.80, MASC clean 23.74, MASC noisy 34.29, MGB-2 17.20); `whisper-large-v3-turbo` 33.30%; `whisper-large-v2` 34.04%; `seamless-m4t-v2-large` 32.55%; `facebook/mms-1b-all` 47.86%; best open model listed `nvidia/conformer-ctc-large-ar` with LM 25.71%. These corpora are MSA, Saudi and broadcast heavy; there is no Lebanese set, which is exactly why the spec insists on the owner's own 50 notes.

**Custom vocabulary.** No boosting. Self-hosted: `initial_prompt` nudges spelling; API: `prompt` (up to 224 tokens on `whisper-1`, longer on `gpt-transcribe`; not on `gpt-4o-transcribe-diarize`). Source: [OpenAI speech-to-text guide](https://developers.openai.com/api/docs/guides/speech-to-text).

**Audio formats.** Self-hosted Whisper loads audio through ffmpeg, so OGG/Opus is fine. The OpenAI API guide: "Supported input formats are mp3, mp4, mpeg, mpga, m4a, wav, and webm", "Files can be up to 25 MB" (ogg and flac are not in the current guide text; transcode voice notes to WAV or M4A, or verify against the API reference). Same source.

**Streaming vs batch.** `whisper-1`: file upload only, supports `timestamp_granularities[]` (`word`, `segment`) and `verbose_json` (`text`, `language`, `duration`, `words[].{word,start,end}`, `segments[]`), `srt`/`vtt`. `gpt-transcribe`, `gpt-4o-transcribe`, `gpt-4o-mini-transcribe`, `gpt-4o-transcribe-diarize`: `stream=true` events; token `logprobs` via `include` on the 4o models; realtime via `gpt-live-transcribe`. Sources: [speech-to-text guide](https://developers.openai.com/api/docs/guides/speech-to-text), [transcriptions API reference](https://developers.openai.com/api/reference/resources/audio/subresources/transcriptions/methods/create).

**Pricing basis.** Per minute: `gpt-4o-transcribe` $0.006, `gpt-4o-mini-transcribe` $0.003, `gpt-4o-transcribe-diarize` $0.006 (pricing page). `whisper-1` at $0.006/min is reported by third parties only; it was not on the fetched pricing page (**unverified**). Source: [OpenAI pricing](https://developers.openai.com/api/docs/pricing). Self-hosted cost is the GPU hour on the UAE server; a 1-minute voice note on large-v3 is seconds of GPU time.

**Data residency.** OpenAI API data residency regions include the United Arab Emirates; `/v1/audio/transcriptions`, `/v1/audio/translations` and `/v1/audio/speech` support storage in "All listed regions" but processing only in "United States, Europe (EEA + Switzerland)"; Realtime is US/EU only; non-US residency needs approval for abuse-monitoring controls and a Modified Retention amendment. Source: [Your data](https://developers.openai.com/api/docs/guides/your-data). For in-region processing the baseline must be self-hosted Whisper on the UAE server (faster-whisper/CTranslate2 or vLLM are common choices; **unverified** here, pick during phase 0). Azure also offers "Whisper via batch transcription" and Whisper on Azure OpenAI, but not in `uaenorth` (regions page above).

## 5. Dialect, code-switching and Arabizi

- Locale strategy per engine for the bake-off: Azure `ar-LB` (primary Levantine), `ar-SY`/`ar-JO` (Levantine alternates), `ar-AE` and `ar-SA` (Gulf; the owner lives and trades in the UAE and uses Gulf vocabulary with customers). Google `ar-LB`, `ar-AE` and language-agnostic `chirp_3`. Scribe `ar`. Whisper `ar`. Run every variant as its own engine directory (`azure_ar_lb`, `azure_ar_ae`, ...) so the ranking shows which locale wins, not just which vendor.
- Code-switching (Arabic sentences with English product names, numbers, "okay", "invoice"): Azure continuous LID with candidates `["ar-LB", "en-US"]` (max 10; not within a sentence) or fast transcription with `locales: []`; Google `chirp_3` language-agnostic transcription; Scribe auto-detects and returns `language_probability`; Whisper detects per 30-second window. Expect English words inside Arabic sentences to be the main error source; the custom vocabulary (Latin spellings for brand names) is the mitigation, and the bake-off's vocabulary-recall score measures it.
- Arabizi ("3ala", "sho el 2akhbar") only exists in *typed* messages; no STT engine emits it. The requirement "parsed as Arabic" lives in the text-understanding layer (the language rules rendered into `prompts/nour.system.md` from SPEC 9 and 18; the prompt does not mention Arabizi yet and should), not in the speech port. `tools/stt_bakeoff.py` keeps Arabizi tokens verbatim and does not transliterate (out of scope by design).

## 6. Running the bake-off (SPEC 9, 16)

1. Collect the 50 owner voice notes (owner input, section 17). They are the owner's own data: keep them encrypted in the UAE region with an explicit owner note that this corpus is retained for the quarterly re-test (the 7-day deletion rule applies to channel audio, not to this consented test set; confirm with the owner).
2. Produce reference transcripts (`refs/<id>.ref.txt`) by a Lebanese-speaking transcriber. Orthography guide: write dialect words as spoken (بدي, هيك, شو), proper names in the canonical spelling of the vocabulary file (Arabic names in Arabic, brand names in Latin letters as the brand writes them), numbers as digits, no diacritics, no punctuation reliance (the scorer strips both).
3. Prepare `config/speech_vocab.txt` (or `.yaml` with categories): company names, products, staff, customers, places, recurring phrases. Load the same list into every engine (phrase list / adaptation / keyterms / prompt) before transcribing.
4. Transcribe every note with every engine configuration into `out/<engine>/<id>.hyp.txt` (raw engine text; the scorer normalises). Record the exact request parameters next to each directory.
5. Score: `python tools/stt_bakeoff.py --refs refs --hyps out/azure_ar_lb out/azure_ar_ae out/google_ar_lb out/scribe out/whisper_large_v3 --custom-vocab config/speech_vocab.txt --report docs/bakeoff/<date>.json`. Ranking is by corpus WER (total errors / total reference words), with CER and vocabulary recall as tie-breakers (CER ignores whitespace so segmentation disagreements such as مابدي vs ما بدي are not double-counted). Use `--show-alignment <id>` to inspect a note.
6. Decide: lowest WER wins (spec). If the margin is under about 2 WER points, prefer the engine with higher vocabulary recall, then lower cost, then UAE residency. Commit the JSON report and the chosen `engine`, `locale`, `model` and vocabulary version to config; the runtime adapter reads that config.
7. Repeat quarterly with the same corpus plus any new notes the owner flags as mis-heard.

Normalisation applied by the scorer to both sides: NFKC; alef variants to bare alef; hamza on waw/yeh to waw/yeh (standalone hamza kept); taa marbuta to haa; alef maqsura to yeh; tatweel and zero-width marks removed; all combining marks (tashkeel) removed; Arabic-Indic and Extended Arabic-Indic digits to ASCII; punctuation to spaces (digit separators dropped so 5,000 equals 5000); Latin case-folded; whitespace collapsed. Every step is a pure function (`normalize_arabic`) with switches, so a stricter variant (for example keeping taa marbuta) is a flag away. The same function should be reused by the runtime adapter to produce `Transcript.normalized_text`.

## 7. Text-to-speech: a Lebanese-accented, locked, synthetic voice

**Rule.** "One licensed or synthetic Arabic voice with a Lebanese accent, chosen once and locked like the face. Never a clone of a real person" (SPEC 3). A stock vendor voice that the vendor licenses is allowed ("licensed"); a voice generated from a text description is allowed ("synthetic"); an instant or professional clone of any individual, including the owner or staff, is not, regardless of consent.

### 7.1 Azure neural voices (recommended first candidate)

- Lebanese: `ar-LB-LaylaNeural` (female), `ar-LB-RamiNeural` (male). Other Levantine: `ar-SY-AmanyNeural`/`ar-SY-LaithNeural`, `ar-JO-SanaNeural`/`ar-JO-TaimNeural`. Gulf: `ar-AE-FatimaNeural`/`ar-AE-HamdanNeural`, `ar-SA-ZariyahNeural`/`ar-SA-HamedNeural`. All Arabic voices are standard neural (no HD or multilingual Arabic voices listed; voice conversion not supported for Arabic). Source: [Language and voice support (TTS tab)](https://learn.microsoft.com/en-us/azure/ai-services/speech-service/language-support?tabs=tts).
- Available in `uaenorth` (neural TTS, batch synthesis). REST: `POST https://uaenorth.tts.speech.microsoft.com/cognitiveservices/v1` with `Content-Type: application/ssml+xml`, `X-Microsoft-OutputFormat`, `User-Agent`; body `<speak version='1.0' xml:lang='ar-LB'><voice name='ar-LB-LaylaNeural'>...</voice></speak>`. Opus outputs for WhatsApp: `ogg-16khz-16bit-mono-opus`, `ogg-24khz-16bit-mono-opus`, `ogg-48khz-16bit-mono-opus` (also `webm-*-opus`, MP3, PCM). Audio per request is capped at 10 minutes; default 30 transactions/second. Sources: [Text to speech REST API](https://learn.microsoft.com/en-us/azure/ai-services/speech-service/rest-text-to-speech), [Quotas and limits](https://learn.microsoft.com/en-us/azure/ai-services/speech-service/speech-services-quotas-and-limits), [Regions](https://learn.microsoft.com/en-us/azure/ai-services/speech-service/regions).
- Price: $15.00 per 1M characters in `uaenorth` (Retail Prices API); 0.5M characters/month free on F0. A 30-second voice-note reply is roughly 400 Arabic characters, so 1,000 replies a month cost well under $10.
- Custom neural voice and personal voice are Limited Access: "Customers must warrant that they have obtained explicit written permission from voice talent prior to creating a voice model" and must upload a recorded consent statement that Microsoft verifies biometrically. Source: [Limited access for custom neural voice](https://learn.microsoft.com/en-us/legal/cognitive-services/speech-service/custom-neural-voice/limited-access-custom-neural-voice). Not needed and not allowed for Nour: use a stock voice.

### 7.2 ElevenLabs

- TTS models with Arabic: `eleven_v3` ("Arabic (ara)" among 70+ languages), `eleven_v4` (90+), `eleven_multilingual_v2` and `eleven_flash_v2_5` ("Arabic (Saudi Arabia, UAE)"); `eleven_turbo_v2_5` is deprecated in favour of Flash. Source: [Models](https://elevenlabs.io/docs/models). No per-dialect voice selector; accent comes from the voice.
- Voice Design generates "entirely new synthetic voices from text descriptions" (description 20 to 1,000 characters, accent and age controllable, three previews) which fits "synthetic ... never a clone" exactly: describe a Lebanese woman in her early thirties, warm, precise. Voice Library voices are other people's professional clones licensed for use (a "licensed" option, check the licence of the specific voice; e.g. community voices labelled "Lebanese accent" exist but are not vetted here). Instant and Professional Voice Cloning are off the table. Source: [Voices](https://elevenlabs.io/docs/capabilities/voices).
- Policy: ElevenLabs prohibits "creating or using ElevenLabs audio output to intentionally replicate the voice of another person: (a) without consent or legal right ... (c) in a manner intended to deceive others about whether the voice was generated by artificial intelligence", and requires AI agents to "clearly and prominently disclose to their users they are interacting with AI". Source: [Use policy](https://elevenlabs.io/use-policy). Both align with SPEC 2 (honesty, disclosure).
- API: `POST /v1/text-to-speech/{voice_id}` with `text`, `model_id`, `language_code` (`ar`), `voice_settings` (`stability`, `similarity_boost`, `style`, `use_speaker_boost`, `speed`), `seed`, `previous_text`/`next_text`; query `output_format` includes `opus_48000_32`, `opus_48000_64`, `mp3_44100_128`, `pcm_16000`, `ulaw_8000`. Returns audio bytes. Source: [Text to speech API](https://elevenlabs.io/docs/api-reference/text-to-speech/convert).
- Price: v3 $0.08 per 1,000 characters, Flash/Turbo $0.04 per 1,000 (v4 was on promotion at $0.022 until 12 Oct 2026); plan tiers Free, Starter $6, Creator $22, Pro $99, Scale $299, Business $990, Enterprise. Source: [API pricing](https://elevenlabs.io/pricing/api). Residency: none in the Middle East (section 4.3); TTS text is Tier 0/1 only, so this is acceptable if the owner approves, but Azure keeps everything in `uaenorth`.

### 7.3 Google Cloud Text-to-Speech

- Only `ar-XA`, which "is Modern Standard Arabic (usually denoted as ar-001)": `ar-XA-Standard-A..D`, `ar-XA-Wavenet-A..D`, and 30 `ar-XA-Chirp3-HD-*` voices. No Levantine or Gulf accent. Source: [Supported voices](https://docs.cloud.google.com/text-to-speech/docs/list-voices-and-types).
- Price per 1M characters: Standard and WaveNet $4, Neural2 $16, Chirp 3: HD $30, Instant custom voice $60, Studio $160; free tiers of 1M (HD, Neural2, Studio) or 4M (Standard, WaveNet) characters/month. Source: [Text-to-Speech pricing](https://cloud.google.com/text-to-speech/pricing).
- Verdict: unsuitable for the owner channel (MSA only); a possible voice for MSA government templates if that ever needs audio, which the spec does not ask for.

### 7.4 Voice lock

Store the chosen voice as `config/voice_lock.yaml`: `engine`, `voice_id` (for Azure the voice name, for ElevenLabs the voice id), `model_id`, `locale: ar-LB`, `voice_settings`, `output_format`, `locked_at`, `approved_by: owner`, `sample_sha256` (hash of a reference rendering of the introduction line). The TTS adapter refuses any request whose `VoiceSpec` does not match the lock, and the lock changes only through the passphrase flow (SPEC 3, 6). The fake enforces the same check so tests cover it.

## 8. Retention and data handling

- Channel audio: delete after 7 days (`config/channels.yaml: audio_retention_days: 7`); keep the transcript, the engine id, the model version, the confidence and the audio SHA-256 in the audit log. Tier 2 content never reaches an out-of-region engine (SPEC 4).
- Azure processes in the resource's region and returns results synchronously for real-time/short/fast; batch transcription results are stored by the service until deleted (set `timeToLive` and delete after download). Google offers data-logging opt-in at a lower price; keep it off. ElevenLabs stores by default in the US unless on an enterprise residency plan; OpenAI stores in-region for UAE projects but processes audio in the US/EU. Vendor retention windows for abuse monitoring were not verified here (**unverified**).
- The bake-off corpus (50 notes) is the owner's data kept with his consent for quarterly re-tests; it must not be used to train or tune any vendor model (disable data sharing / logging switches on every vendor account).

## 9. Port design

Both ports are `typing.Protocol`s so the real adapters (Azure, Google, ElevenLabs, Whisper) and the in-memory fakes are interchangeable; the agent loop and tests only ever see these types. Suggested module: `nour/ports/speech.py`; adapters under `nour/adapters/speech/<vendor>.py`.

```python
from __future__ import annotations

import hashlib
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass, field
from typing import Literal, Protocol

AudioMime = Literal["audio/ogg; codecs=opus", "audio/wav", "audio/mpeg", "audio/mp4", "audio/webm"]


@dataclass(frozen=True)
class AudioInput:
    data: bytes
    mime_type: AudioMime
    duration_s: float | None = None          # from the container when known
    sample_rate_hz: int | None = None
    channels: int = 1
    source: str = "whatsapp_voice_note"      # for the audit log reason line
    sha256: str = field(default="", compare=False)

    def digest(self) -> str:
        return self.sha256 or hashlib.sha256(self.data).hexdigest()


@dataclass(frozen=True)
class SttOptions:
    locale_hints: Sequence[str] = ("ar-LB",)   # engine maps to its own codes; ["ar-LB", "en-US"] enables LID
    vocabulary: Sequence[str] = ()             # phrase list / adaptation / keyterms / prompt
    word_timestamps: bool = False
    diarize: bool = False
    timeout_s: float = 30.0


@dataclass(frozen=True)
class TranscriptWord:
    text: str
    start_s: float | None
    end_s: float | None
    confidence: float | None                   # None when the engine gives none


@dataclass(frozen=True)
class Transcript:
    text: str                                  # engine display text, untouched
    normalized_text: str                       # tools/stt_bakeoff.normalize_arabic(text)
    language: str                              # BCP-47 as reported or as hinted
    confidence: float | None                   # 0..1 utterance-level; None if the engine has none
    engine: str                                # "azure" | "google" | "elevenlabs" | "whisper" | "fake"
    model: str                                 # e.g. "ar-LB/2026-09", "chirp_3", "scribe_v2", "large-v3"
    duration_s: float | None
    words: tuple[TranscriptWord, ...] = ()
    alternatives: tuple[str, ...] = ()         # N-best when available
    audio_sha256: str = ""
    raw: dict | None = None                    # vendor payload for debugging; never logged to the audit log

    def needs_readback(self, threshold: float = 0.80) -> bool:
        """True when the agent must read back before acting even on a Tier A command."""
        return self.confidence is None or self.confidence < threshold


class SpeechError(Exception):
    retryable: bool = False


class AudioFormatError(SpeechError): ...          # unsupported container/codec, corrupt file
class AudioTooLongError(SpeechError): ...         # over the engine or policy limit
class NoSpeechDetected(SpeechError): ...          # silence, noise only (Azure NoMatch/InitialSilenceTimeout)
class EngineUnavailable(SpeechError):             # 5xx, network, region outage
    retryable = True
class EngineQuotaExceeded(SpeechError):           # 429, concurrency or spend cap
    retryable = True
class EngineTimeout(SpeechError):
    retryable = True
class VocabularyTooLarge(SpeechError): ...        # over 2,000 phrases (Azure) / 1,000 (Scribe) etc.
class LockedVoiceMismatch(SpeechError): ...       # TTS request for a voice other than the locked one


class SpeechToTextPort(Protocol):
    engine_id: str                             # stable id written to the audit log

    async def transcribe(self, audio: AudioInput, options: SttOptions) -> Transcript: ...

    async def transcribe_stream(
        self, pcm16_chunks: AsyncIterator[bytes], options: SttOptions
    ) -> AsyncIterator[Transcript]:
        """Optional. Yields partial transcripts, final one last. Adapters without
        streaming raise NotImplementedError; the agent falls back to transcribe()."""
        ...

    async def health(self) -> bool: ...


@dataclass(frozen=True)
class VoiceSpec:
    engine: str
    voice_id: str                              # "ar-LB-LaylaNeural" or an ElevenLabs voice id
    model_id: str                              # "neural" / "eleven_v3" ...
    locale: str = "ar-LB"
    settings: tuple[tuple[str, float], ...] = ()   # sorted (name, value) pairs, hashed into the lock


@dataclass(frozen=True)
class TtsOptions:
    output_mime: AudioMime = "audio/ogg; codecs=opus"
    sample_rate_hz: int = 48000
    speed: float = 1.0
    max_chars: int = 2000                      # policy cap per voice note
    timeout_s: float = 30.0


@dataclass(frozen=True)
class SynthesizedAudio:
    data: bytes
    mime_type: AudioMime
    duration_s: float | None
    voice: VoiceSpec
    chars_billed: int
    engine: str
    text_sha256: str


class TextToSpeechPort(Protocol):
    engine_id: str
    locked_voice: VoiceSpec

    async def synthesize(self, text: str, voice: VoiceSpec, options: TtsOptions) -> SynthesizedAudio:
        """Raises LockedVoiceMismatch if voice != self.locked_voice."""
        ...

    async def health(self) -> bool: ...
```

Adapter obligations (same for every vendor):

- Decode or pass through OGG/Opus as the vendor allows; transcode with ffmpeg otherwise. Never send audio longer than the policy limit (suggest 10 minutes) without chunking.
- Map vendor statuses to the error classes above; mark 429/5xx/timeouts retryable; the watchdog counts failures.
- Fill `confidence` honestly: Azure phrase `confidence` or `NBest[0].Confidence`; Google `alternatives[0].confidence` (ordinal only on `chirp_3`); Scribe mean of word `logprob` mapped through `exp()` or `language_probability` as a floor; Whisper `avg_logprob` per segment mapped through `exp()` (self-hosted) or None (API without logprobs). Document the mapping in the adapter docstring so the read-back threshold can be tuned per engine.
- Load the vocabulary at request time (phrase list / adaptation / keyterms / prompt); raise `VocabularyTooLarge` instead of silently truncating.
- Log engine id, model, region, duration, confidence, audio SHA-256 and the one-sentence reason; never log `raw` or the audio bytes.

### 9.1 In-memory fakes (what tests rely on)

`FakeSpeechToText` (deterministic, no I/O):

- Scripted results keyed by `AudioInput.digest()` or by call order: `Transcript` with `text`, `confidence` (default 0.93), optional `words` with monotonically increasing timestamps when `options.word_timestamps` is set, `language` echoing the first locale hint, `engine="fake"`, `model="fake-1"`.
- Failure modes selectable per call or per digest: `AudioFormatError` (for example when `mime_type` is not in its accepted set), `AudioTooLongError` when `duration_s` exceeds a configurable limit, `NoSpeechDetected` for empty `data`, `EngineUnavailable` / `EngineQuotaExceeded` / `EngineTimeout` (retryable, with an optional "fail N times then succeed" counter so retry logic is testable), `VocabularyTooLarge` when `len(options.vocabulary)` exceeds a configurable cap.
- Low-confidence simulation: a `confidence` below the read-back threshold so the agent's read-back path is exercised; a scripted `alternatives` tuple for N-best handling.
- Records every call (`calls: list[tuple[AudioInput, SttOptions]]`) so tests can assert the vocabulary was passed and that Tier 2 audio never reached a non-in-region engine id.
- `normalized_text` must be produced with the same `normalize_arabic` function as the bake-off tool, so a test that feeds "بَدّي أحجز" and asserts `normalized_text == "بدي احجز"` is meaningful.

`FakeTextToSpeech`:

- Returns deterministic bytes (for example an `OggS` header followed by the SHA-256 of `text + voice_id`), `duration_s` estimated from character count (about 13 characters per second for Arabic is a fine stub), `chars_billed == len(text)`.
- Enforces the voice lock exactly like the real adapters: `LockedVoiceMismatch` for any `VoiceSpec` other than `locked_voice`.
- Failure modes: `EngineQuotaExceeded` (429), `EngineTimeout`, text over `max_chars`, empty text (`ValueError`).
- Records calls and total characters so the budget tests (AI-model budget line, section 17) can assert spend.

## 10. Open items and unverified points

1. Phrase-list effectiveness on `ar-LB` in Azure (locale table flags phrase list under `ar-SA` only): measure in the bake-off.
2. Azure batch and fast transcription meters for `uaenorth` were not returned by the Retail Prices API; batch price quoted from `eastus` ($0.18/h). Fast transcription is not offered in `uaenorth` at all.
3. Google `auto_decoding_config` on Ogg/Opus: unverified; use `explicit_decoding_config`.
4. Google Chirp pricing category (Standard) comes from a Google blog post, not the pricing table.
5. ElevenLabs file limits differ between the capabilities page (3 GB / 10 h) and the API reference (5 GB); zero-retention terms only seen in launch material.
6. OpenAI `whisper-1` per-minute price not on the fetched pricing page (third-party sources say $0.006/min); the current API guide omits ogg/flac from its format list.
7. Whisper licence: MIT (GitHub) versus Apache-2.0 (Hugging Face card).
8. No independent Lebanese-dialect benchmark exists for any engine; the leaderboard numbers above are MSA/Gulf/broadcast. The 50-note bake-off is the only evidence that counts.
9. Owner decisions needed: whether Google/ElevenLabs may process his voice notes outside the UAE for the bake-off; retention of the 50-note corpus for quarterly re-tests; the AI-model budget line.
10. Self-hosted Whisper runtime (faster-whisper, vLLM, or the reference implementation) and GPU sizing on the UAE server: choose in phase 0.

## Sources

- Azure: [language support (STT)](https://learn.microsoft.com/en-us/azure/ai-services/speech-service/language-support?tabs=stt), [language support (TTS)](https://learn.microsoft.com/en-us/azure/ai-services/speech-service/language-support?tabs=tts), [phrase list](https://learn.microsoft.com/en-us/azure/ai-services/speech-service/improve-accuracy-phrase-list), [fast transcription](https://learn.microsoft.com/en-us/azure/ai-services/speech-service/fast-transcription-create), [compressed audio input](https://learn.microsoft.com/en-us/azure/ai-services/speech-service/how-to-use-codec-compressed-audio-input-streams), [batch transcription audio data](https://learn.microsoft.com/en-us/azure/ai-services/speech-service/batch-transcription-audio-data), [REST short audio](https://learn.microsoft.com/en-us/azure/ai-services/speech-service/rest-speech-to-text-short), [REST text to speech](https://learn.microsoft.com/en-us/azure/ai-services/speech-service/rest-text-to-speech), [language identification](https://learn.microsoft.com/en-us/azure/ai-services/speech-service/language-identification), [regions](https://learn.microsoft.com/en-us/azure/ai-services/speech-service/regions), [quotas and limits](https://learn.microsoft.com/en-us/azure/ai-services/speech-service/speech-services-quotas-and-limits), [custom neural voice limited access](https://learn.microsoft.com/en-us/legal/cognitive-services/speech-service/custom-neural-voice/limited-access-custom-neural-voice), [pricing page](https://azure.microsoft.com/en-us/pricing/details/cognitive-services/speech-services/), [Retail Prices API](https://prices.azure.com/api/retail/prices).
- Google: [supported languages](https://docs.cloud.google.com/speech-to-text/v2/docs/speech-to-text-supported-languages), [Chirp 3](https://docs.cloud.google.com/speech-to-text/docs/models/chirp-3), [model adaptation](https://docs.cloud.google.com/speech-to-text/v2/docs/adaptation-model), [encodings](https://docs.cloud.google.com/speech-to-text/v2/docs/encoding), [quotas](https://docs.cloud.google.com/speech-to-text/quotas), [regional endpoints](https://docs.cloud.google.com/speech-to-text/docs/endpoints), [recognize reference](https://docs.cloud.google.com/speech-to-text/docs/reference/rest/v2/projects.locations.recognizers/recognize), [STT pricing](https://cloud.google.com/speech-to-text/pricing), [TTS voices](https://docs.cloud.google.com/text-to-speech/docs/list-voices-and-types), [TTS pricing](https://cloud.google.com/text-to-speech/pricing).
- ElevenLabs: [speech to text](https://elevenlabs.io/docs/capabilities/speech-to-text), [convert API](https://elevenlabs.io/docs/api-reference/speech-to-text/convert), [realtime cookbook](https://elevenlabs.io/docs/developers/guides/cookbooks/speech-to-text/streaming), [models](https://elevenlabs.io/docs/models), [voices](https://elevenlabs.io/docs/capabilities/voices), [text to speech API](https://elevenlabs.io/docs/api-reference/text-to-speech/convert), [API pricing](https://elevenlabs.io/pricing/api), [data residency](https://elevenlabs.io/docs/overview/administration/data-residency), [use policy](https://elevenlabs.io/use-policy), [Scribe v2 launch](https://elevenlabs.io/blog/introducing-scribe-v2).
- Whisper / OpenAI: [whisper README](https://github.com/openai/whisper), [large-v3 model card](https://huggingface.co/openai/whisper-large-v3), [tokenizer.py](https://raw.githubusercontent.com/openai/whisper/main/whisper/tokenizer.py), [Open Universal Arabic ASR Leaderboard](https://arxiv.org/html/2412.13788), [speech-to-text guide](https://developers.openai.com/api/docs/guides/speech-to-text), [transcriptions reference](https://developers.openai.com/api/reference/resources/audio/subresources/transcriptions/methods/create), [pricing](https://developers.openai.com/api/docs/pricing), [your data / residency](https://developers.openai.com/api/docs/guides/your-data).
- WhatsApp: [media reference](https://developers.facebook.com/docs/whatsapp/cloud-api/reference/media), [audio webhook](https://developers.facebook.com/documentation/business-messaging/whatsapp/webhooks/reference/messages/audio/), [audio messages](https://developers.facebook.com/documentation/business-messaging/whatsapp/messages/audio-messages).
