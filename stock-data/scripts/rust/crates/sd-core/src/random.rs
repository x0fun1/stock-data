//! CPython-compatible deterministic pseudo-random generator (MT19937).
//!
//! Research code that resamples labels must reproduce the legacy Python streams
//! exactly, otherwise every bootstrap interval changes. Only the documented
//! `random.Random` surface used by the legacy helpers is implemented:
//! `random()`, `getrandbits(k)`, `randrange(stop)` and `gauss(mu, sigma)`.
//!
//! Not a cryptographic generator; never use it for secrets.

/// The 624-word Mersenne Twister state and Python's Box-Muller cache.
#[derive(Debug, Clone)]
pub struct PythonRandom {
    state: [u32; 624],
    index: usize,
    gauss_next: Option<f64>,
}

const N: usize = 624;
const M: usize = 397;
const MATRIX_A: u32 = 0x9908_b0df;
const UPPER_MASK: u32 = 0x8000_0000;
const LOWER_MASK: u32 = 0x7fff_ffff;
const TWO_PI: f64 = std::f64::consts::TAU;

impl PythonRandom {
    /// `random.Random(seed)` for a non-negative integer seed.
    pub fn new(seed: u64) -> Self {
        let mut key = Vec::with_capacity(2);
        let mut value = seed;
        loop {
            key.push((value & 0xffff_ffff) as u32);
            value >>= 32;
            if value == 0 {
                break;
            }
        }
        Self::from_key(&key)
    }

    /// `random.Random()` seeded through the C `init_by_array` path.
    pub fn from_key(key: &[u32]) -> Self {
        let mut random = Self {
            state: [0; N],
            index: N,
            gauss_next: None,
        };
        random.init_genrand(19_650_218);
        let mut i = 1usize;
        let mut j = 0usize;
        let mut k = N.max(key.len());
        while k > 0 {
            random.state[i] = (random.state[i]
                ^ ((random.state[i - 1] ^ (random.state[i - 1] >> 30)).wrapping_mul(1_664_525)))
            .wrapping_add(key[j])
            .wrapping_add(j as u32);
            i += 1;
            j += 1;
            if i >= N {
                random.state[0] = random.state[N - 1];
                i = 1;
            }
            if j >= key.len() {
                j = 0;
            }
            k -= 1;
        }
        let mut k = N - 1;
        while k > 0 {
            random.state[i] = (random.state[i]
                ^ ((random.state[i - 1] ^ (random.state[i - 1] >> 30))
                    .wrapping_mul(1_566_083_941)))
            .wrapping_sub(i as u32);
            i += 1;
            if i >= N {
                random.state[0] = random.state[N - 1];
                i = 1;
            }
            k -= 1;
        }
        random.state[0] = 0x8000_0000;
        random
    }

    fn init_genrand(&mut self, seed: u32) {
        self.state[0] = seed;
        for index in 1..N {
            let previous = self.state[index - 1];
            self.state[index] = 1_812_433_253u32
                .wrapping_mul(previous ^ (previous >> 30))
                .wrapping_add(index as u32);
        }
        self.index = N;
    }

    fn twist(&mut self) {
        for kk in 0..N {
            let y = (self.state[kk] & UPPER_MASK) | (self.state[(kk + 1) % N] & LOWER_MASK);
            let mut next = self.state[(kk + M) % N] ^ (y >> 1);
            if !y.is_multiple_of(2) {
                next ^= MATRIX_A;
            }
            self.state[kk] = next;
        }
        self.index = 0;
    }

    /// One raw 32-bit word of the stream.
    pub fn genrand_uint32(&mut self) -> u32 {
        if self.index >= N {
            self.twist();
        }
        let mut y = self.state[self.index];
        self.index += 1;
        y ^= y >> 11;
        y ^= (y << 7) & 0x9d2c_5680;
        y ^= (y << 15) & 0xefc6_0000;
        y ^= y >> 18;
        y
    }

    /// `random.random()`: 53 random bits divided by 2**53.
    pub fn random(&mut self) -> f64 {
        let a = (self.genrand_uint32() >> 5) as f64;
        let b = (self.genrand_uint32() >> 6) as f64;
        (a * 67_108_864.0 + b) / 9_007_199_254_740_992.0
    }

    /// `random.getrandbits(k)` for `k <= 32` (enough for every legacy sampler).
    pub fn getrandbits(&mut self, k: u32) -> u64 {
        if k == 0 {
            return 0;
        }
        if k <= 32 {
            return u64::from(self.genrand_uint32() >> (32 - k));
        }
        let mut value: u64 = 0;
        let mut filled = 0u32;
        while filled < k {
            let take = (k - filled).min(32);
            let word = u64::from(self.genrand_uint32()) >> (32 - take);
            value |= word << filled;
            filled += take;
        }
        value
    }

    /// `random._randbelow_with_getrandbits`: rejection sampling, no modulo bias.
    pub fn randrange(&mut self, stop: u64) -> u64 {
        assert!(stop > 0, "empty range for randrange()");
        let bits = 64 - stop.leading_zeros();
        loop {
            let candidate = self.getrandbits(bits);
            if candidate < stop {
                return candidate;
            }
        }
    }

    /// `random.gauss(mu, sigma)` with Python's cached Box-Muller pair.
    pub fn gauss(&mut self, mu: f64, sigma: f64) -> f64 {
        let z = match self.gauss_next.take() {
            Some(z) => z,
            None => {
                let x2pi = self.random() * TWO_PI;
                let g2rad = (-2.0 * (1.0 - self.random()).ln()).sqrt();
                let z = x2pi.cos() * g2rad;
                self.gauss_next = Some(x2pi.sin() * g2rad);
                z
            }
        };
        mu + z * sigma
    }
}
