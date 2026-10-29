//! Reference primitives. Profile versions describe algorithms, not package versions.
use rustfft::{num_complex::Complex, FftPlanner};
use serde::{Deserialize, Serialize};
use std::collections::{BTreeSet, VecDeque};
use std::io::Read;
use unicode_normalization::UnicodeNormalization;
use unicode_segmentation::UnicodeSegmentation;

pub const TEXT_PROFILE: &str = "trace.text.nfc-minhash128-winnow32.v0";
pub const CODE_PROFILE: &str = "trace.code.nfc-minhash128-winnow32.v0";
pub const AUDIO_PROFILE: &str = "trace.audio.landmarks-11025-2048-512.v0";

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct Anchor {
    pub hash: String,
    pub position: u64,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct TextFingerprint {
    pub profile: String,
    pub canonical_digest: String,
    pub minhash: Vec<String>,
    pub anchors: Vec<Anchor>,
    pub units: u64,
    pub distinct_shingles: usize,
    pub low_information: bool,
    pub truncated: bool,
}

fn mix(mut x: u64) -> u64 {
    x = x.wrapping_add(0x9e3779b97f4a7c15);
    x = (x ^ (x >> 30)).wrapping_mul(0xbf58476d1ce4e5b9);
    x = (x ^ (x >> 27)).wrapping_mul(0x94d049bb133111eb);
    x ^ (x >> 31)
}

pub fn text_fingerprint(input: &str, code: bool, max_anchors: usize) -> TextFingerprint {
    let normalized: String = input
        .replace("\r\n", "\n")
        .replace('\r', "\n")
        .nfc()
        .collect();
    let canonical = if code {
        normalized
    } else {
        normalized.split_whitespace().collect::<Vec<_>>().join(" ")
    };
    // No stemming, case folding, identifier replacement or learned tokenization.
    let owned: Vec<String>;
    let tokens: Vec<&str> = if code {
        owned = canonical.chars().map(|c| c.to_string()).collect();
        owned.iter().map(String::as_str).collect()
    } else {
        canonical.unicode_words().collect()
    };
    let mut signature = [u64::MAX; 128];
    let mut window: VecDeque<(usize, u64)> = VecDeque::new();
    let mut distinct = BTreeSet::new();
    let mut anchors = Vec::new();
    let mut last = None;
    let mut truncated = false;
    for (pos, shingle) in tokens.windows(5).enumerate() {
        let mut hasher = blake3::Hasher::new_derive_key("trace.text.shingle.v0");
        for word in shingle {
            hasher.update(&(word.len() as u64).to_le_bytes());
            hasher.update(word.as_bytes());
        }
        let h = u64::from_le_bytes(hasher.finalize().as_bytes()[..8].try_into().unwrap());
        // Only retain enough distinct values for the low-information decision.
        if distinct.len() < 64 {
            distinct.insert(h);
        }
        for (i, value) in signature.iter_mut().enumerate() {
            *value = (*value).min(mix(h ^ mix(i as u64)));
        }
        // Rightmost minimum in each 32-shingle window. O(n), bounded queue.
        while window.back().is_some_and(|(_, v)| *v >= h) {
            window.pop_back();
        }
        window.push_back((pos, h));
        while window
            .front()
            .is_some_and(|(p, _)| p.saturating_add(32) <= pos)
        {
            window.pop_front();
        }
        if pos >= 31 {
            let (p, v) = *window.front().unwrap();
            if last != Some(p) {
                if anchors.len() < max_anchors {
                    anchors.push(Anchor {
                        hash: format!("{v:016x}"),
                        position: p as u64,
                    });
                } else {
                    truncated = true;
                }
                last = Some(p);
            }
        }
    }
    TextFingerprint {
        profile: if code { CODE_PROFILE } else { TEXT_PROFILE }.into(),
        canonical_digest: blake3::hash(canonical.as_bytes()).to_hex().to_string(),
        minhash: if tokens.len() >= 5 {
            signature.iter().map(|h| format!("{h:016x}")).collect()
        } else {
            vec![]
        },
        anchors,
        units: tokens.len() as u64,
        distinct_shingles: distinct.len(),
        low_information: tokens.len() < 36 || distinct.len() < 16,
        truncated,
    }
}

pub fn digest_reader<R: Read>(mut reader: R) -> std::io::Result<(String, u64)> {
    let mut h = blake3::Hasher::new();
    let mut buffer = [0u8; 1024 * 1024];
    let mut size = 0;
    loop {
        let n = reader.read(&mut buffer)?;
        if n == 0 {
            break;
        }
        h.update(&buffer[..n]);
        size += n as u64;
    }
    Ok((h.finalize().to_hex().to_string(), size))
}

#[derive(Clone, Serialize)]
pub struct AudioFingerprint {
    pub profile: String,
    pub landmarks: Vec<Anchor>,
    pub samples: u64,
    pub sample_rate: u32,
    pub hop: u32,
    pub low_information: bool,
    pub truncated: bool,
}

pub struct AudioState {
    samples: VecDeque<f32>,
    frames: VecDeque<(u64, Vec<usize>)>,
    landmarks: Vec<Anchor>,
    frame: u64,
    total: u64,
    max: usize,
    truncated: bool,
    fft: std::sync::Arc<dyn rustfft::Fft<f32>>,
}
impl AudioState {
    pub fn new(max: usize) -> Self {
        Self {
            samples: VecDeque::new(),
            frames: VecDeque::new(),
            landmarks: vec![],
            frame: 0,
            total: 0,
            max,
            truncated: false,
            fft: FftPlanner::new().plan_fft_forward(2048),
        }
    }
    pub fn push(&mut self, pcm: &[f32]) {
        self.total += pcm.len() as u64;
        self.samples.extend(pcm);
        while self.samples.len() >= 2048 {
            let mut fft: Vec<Complex<f32>> = self
                .samples
                .iter()
                .take(2048)
                .enumerate()
                .map(|(i, &x)| {
                    let w = 0.5 - 0.5 * (std::f32::consts::TAU * i as f32 / 2047.0).cos();
                    Complex::new(x * w, 0.0)
                })
                .collect();
            self.fft.process(&mut fft);
            let power: Vec<f32> = fft.iter().take(1024).map(|x| x.norm_sqr()).collect();
            let mut peaks: Vec<usize> = (20..1000)
                .filter(|&i| power[i] > 0.01 && power[i] > power[i - 1] && power[i] > power[i + 1])
                .collect();
            peaks.sort_by(|&a, &b| power[b].total_cmp(&power[a]).then(a.cmp(&b)));
            peaks.truncate(3);
            // Pair peaks at fixed time separations; matching verifies offset consensus.
            for (time, old) in &self.frames {
                let dt = self.frame - time;
                if [3, 7, 15].contains(&dt) {
                    for &a in old.iter().take(2) {
                        for &b in peaks.iter().take(2) {
                            let value = ((a as u64 / 4) << 24) | ((b as u64 / 4) << 8) | dt;
                            if self.landmarks.len() < self.max {
                                self.landmarks.push(Anchor {
                                    hash: format!("{value:016x}"),
                                    position: *time,
                                });
                            } else {
                                self.truncated = true;
                            }
                        }
                    }
                }
            }
            self.frames.push_back((self.frame, peaks));
            while self
                .frames
                .front()
                .is_some_and(|(t, _)| *t + 15 <= self.frame)
            {
                self.frames.pop_front();
            }
            self.frame += 1;
            self.samples.drain(..512);
        }
    }
    pub fn result(&self) -> AudioFingerprint {
        AudioFingerprint {
            profile: AUDIO_PROFILE.into(),
            landmarks: self.landmarks.clone(),
            samples: self.total,
            sample_rate: 11025,
            hop: 512,
            low_information: self.landmarks.len() < 100,
            truncated: self.truncated,
        }
    }
}

#[cfg(feature = "python")]
mod python {
    use super::*;
    use pyo3::prelude::*;
    #[pyfunction]
    fn digest_bytes(data: &[u8]) -> String {
        blake3::hash(data).to_hex().to_string()
    }
    #[pyfunction]
    fn digest_file(py: Python<'_>, path: String) -> PyResult<(String, u64)> {
        py.allow_threads(|| digest_reader(std::fs::File::open(path)?))
            .map_err(Into::into)
    }
    #[pyfunction]
    fn fingerprint_text(py: Python<'_>, text: String, code: bool, max_anchors: usize) -> String {
        py.allow_threads(|| {
            serde_json::to_string(&text_fingerprint(&text, code, max_anchors)).unwrap()
        })
    }
    #[pyclass]
    struct Hasher {
        inner: blake3::Hasher,
    }
    #[pymethods]
    impl Hasher {
        #[new]
        fn new() -> Self {
            Self {
                inner: blake3::Hasher::new(),
            }
        }
        fn update(&mut self, data: &[u8]) {
            self.inner.update(data);
        }
        fn hexdigest(&self) -> String {
            self.inner.finalize().to_hex().to_string()
        }
    }
    #[pyclass]
    struct AudioHasher {
        inner: AudioState,
    }
    #[pymethods]
    impl AudioHasher {
        #[new]
        fn new(max_landmarks: usize) -> Self {
            Self {
                inner: AudioState::new(max_landmarks),
            }
        }
        fn push(&mut self, py: Python<'_>, pcm: Vec<f32>) {
            py.allow_threads(|| self.inner.push(&pcm));
        }
        fn finish(&self) -> String {
            serde_json::to_string(&self.inner.result()).unwrap()
        }
    }
    #[pymodule]
    fn _native(m: &Bound<'_, PyModule>) -> PyResult<()> {
        m.add_function(wrap_pyfunction!(digest_bytes, m)?)?;
        m.add_function(wrap_pyfunction!(digest_file, m)?)?;
        m.add_function(wrap_pyfunction!(fingerprint_text, m)?)?;
        m.add_class::<Hasher>()?;
        m.add_class::<AudioHasher>()?;
        Ok(())
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn vectors() {
        assert_eq!(
            digest_reader(&b""[..]).unwrap().0,
            "af1349b9f5f9a1a6a0404dea36dcc9499bcb25c9adc112b7cc9a93cae41f3262"
        );
        assert_eq!(
            digest_reader(&b"abc"[..]).unwrap().0,
            "6437b3ac38465133ffb63b75273a8db548c558465d79db03fd359c6cd5bd9d85"
        );
    }
    #[test]
    fn normalization() {
        assert_eq!(
            text_fingerprint("cafe\u{301}\r\nhello", false, 100).canonical_digest,
            text_fingerprint("café hello", false, 100).canonical_digest
        );
        assert_ne!(
            text_fingerprint("if x:\n  y()", true, 100).canonical_digest,
            text_fingerprint("if x:\ny()", true, 100).canonical_digest
        );
    }
    #[test]
    fn bounds_and_silence() {
        let t = (0..1000)
            .map(|i| format!("word{i}"))
            .collect::<Vec<_>>()
            .join(" ");
        let f = text_fingerprint(&t, false, 2);
        assert_eq!(f.anchors.len(), 2);
        assert!(f.truncated);
        let mut a = AudioState::new(100);
        a.push(&[0.0; 5000]);
        assert!(a.result().low_information);
    }
    #[test]
    fn audio_chunk_invariance() {
        let x: Vec<f32> = (0..20000)
            .map(|i| ((i as f32 * 0.11).sin() + (i as f32 * 0.37).sin()) * 0.1)
            .collect();
        let mut a = AudioState::new(10000);
        a.push(&x);
        let mut b = AudioState::new(10000);
        for chunk in x.chunks(333) {
            b.push(chunk);
        }
        assert_eq!(
            serde_json::to_string(&a.result()).unwrap(),
            serde_json::to_string(&b.result()).unwrap()
        );
    }
}
