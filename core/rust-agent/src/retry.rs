//! Bounded exponential backoff with jitter.

use std::time::Duration;

use rand::Rng;

use crate::config::RetryCfg;

#[derive(Debug, Clone)]
pub struct Backoff {
    pub max_attempts: u32,
    initial: Duration,
    max: Duration,
    multiplier: f64,
    jitter: f64,
}

impl Backoff {
    pub fn from_config(cfg: &RetryCfg) -> Self {
        Self {
            max_attempts: cfg.max_attempts.max(1),
            initial: Duration::from_millis(cfg.initial_backoff_ms),
            max: Duration::from_millis(cfg.max_backoff_ms),
            multiplier: cfg.multiplier.max(1.0),
            jitter: cfg.jitter.clamp(0.0, 1.0),
        }
    }

    /// Delay before retry number `attempt` (1-based).
    pub fn delay(&self, attempt: u32) -> Duration {
        let exponent = attempt.saturating_sub(1).min(30) as i32;
        let base = (self.initial.as_secs_f64() * self.multiplier.powi(exponent)).min(self.max.as_secs_f64());
        let spread = base * self.jitter;
        let jittered = if spread > 0.0 { base + rand::thread_rng().gen_range(-spread..=spread) } else { base };
        Duration::from_secs_f64(jittered.max(0.0))
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn grows_and_caps() {
        let b = Backoff::from_config(&RetryCfg {
            max_attempts: 5,
            initial_backoff_ms: 100,
            max_backoff_ms: 350,
            multiplier: 2.0,
            jitter: 0.0,
        });
        let delays: Vec<u128> = (1..=5).map(|n| b.delay(n).as_millis()).collect();
        assert_eq!(delays, vec![100, 200, 350, 350, 350]);
    }
}
