//! Circuit breaker and exponential backoff used by the gateway.

use std::time::{Duration, Instant};

use rand::Rng;

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum BreakerState {
    Closed,
    Open,
    HalfOpen,
}

impl BreakerState {
    pub fn as_str(self) -> &'static str {
        match self {
            BreakerState::Closed => "closed",
            BreakerState::Open => "open",
            BreakerState::HalfOpen => "half_open",
        }
    }
}

#[derive(Debug, Clone, Copy)]
pub struct BreakerSettings {
    pub failure_threshold: u32,
    pub open_duration: Duration,
    pub max_open_duration: Duration,
}

/// Classic three-state breaker.
///
/// CLOSED: calls pass; `failure_threshold` consecutive failures open the breaker.
/// OPEN: calls are rejected until `open_duration` elapsed, then one probe is allowed (HALF_OPEN).
/// HALF_OPEN: the probe's success closes the breaker; its failure re-opens it with a doubled open
/// duration, capped at `max_open_duration`.
///
/// Time is passed in (`now`) so the state machine is testable without sleeping.
#[derive(Debug)]
pub struct CircuitBreaker {
    settings: BreakerSettings,
    state: BreakerState,
    failures: u32,
    opened_at: Instant,
    open_duration: Duration,
    probe_in_flight: bool,
}

impl CircuitBreaker {
    pub fn new(settings: BreakerSettings) -> Self {
        Self {
            settings,
            state: BreakerState::Closed,
            failures: 0,
            opened_at: Instant::now(),
            open_duration: settings.open_duration,
            probe_in_flight: false,
        }
    }

    pub fn state_at(&self, now: Instant) -> BreakerState {
        if self.state == BreakerState::Open && now.saturating_duration_since(self.opened_at) >= self.open_duration {
            return BreakerState::HalfOpen;
        }
        self.state
    }

    pub fn state(&self) -> BreakerState {
        self.state_at(Instant::now())
    }

    pub fn until_probe_at(&self, now: Instant) -> Duration {
        if self.state != BreakerState::Open {
            return Duration::ZERO;
        }
        self.open_duration.saturating_sub(now.saturating_duration_since(self.opened_at))
    }

    pub fn allow_at(&mut self, now: Instant) -> bool {
        match self.state_at(now) {
            BreakerState::Closed => true,
            BreakerState::HalfOpen if !self.probe_in_flight => {
                self.state = BreakerState::HalfOpen;
                self.probe_in_flight = true;
                true
            }
            _ => false,
        }
    }

    pub fn allow(&mut self) -> bool {
        self.allow_at(Instant::now())
    }

    pub fn record_success(&mut self) {
        self.state = BreakerState::Closed;
        self.failures = 0;
        self.probe_in_flight = false;
        self.open_duration = self.settings.open_duration;
    }

    pub fn record_failure_at(&mut self, now: Instant) {
        if self.state_at(now) == BreakerState::HalfOpen {
            self.open_duration = (self.open_duration * 2).min(self.settings.max_open_duration);
            self.trip(now);
            return;
        }
        self.failures += 1;
        if self.failures >= self.settings.failure_threshold {
            self.trip(now);
        }
    }

    pub fn record_failure(&mut self) {
        self.record_failure_at(Instant::now())
    }

    /// The probe ended without a verdict (e.g. capability mismatch): allow another probe.
    pub fn release_probe(&mut self) {
        self.probe_in_flight = false;
    }

    /// Open immediately (used when the managed process is known to be dead).
    pub fn force_open(&mut self) {
        self.trip(Instant::now());
    }

    fn trip(&mut self, now: Instant) {
        self.state = BreakerState::Open;
        self.opened_at = now;
        self.failures = 0;
        self.probe_in_flight = false;
    }
}

#[derive(Debug, Clone, Copy)]
pub struct BackoffPolicy {
    pub max_attempts: u32,
    pub initial: Duration,
    pub max: Duration,
    pub multiplier: f64,
    pub jitter: f64,
}

impl BackoffPolicy {
    /// Delay before retry number `attempt` (1-based), with symmetric jitter.
    pub fn delay(&self, attempt: u32) -> Duration {
        let base = (self.initial.as_secs_f64() * self.multiplier.powi(attempt as i32 - 1)).min(self.max.as_secs_f64());
        let spread = base * self.jitter;
        let offset = if spread > 0.0 { rand::thread_rng().gen_range(-spread..=spread) } else { 0.0 };
        Duration::from_secs_f64((base + offset).max(0.0))
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn breaker_half_open_probe_and_backoff_growth() {
        let t0 = Instant::now();
        let s = |secs: u64| t0 + Duration::from_secs(secs);
        let mut breaker = CircuitBreaker::new(BreakerSettings {
            failure_threshold: 1,
            open_duration: Duration::from_secs(10),
            max_open_duration: Duration::from_secs(25),
        });
        breaker.record_failure_at(s(0));
        assert_eq!(breaker.state_at(s(0)), BreakerState::Open);
        assert!(!breaker.allow_at(s(0)));
        assert_eq!(breaker.state_at(s(10)), BreakerState::HalfOpen);
        assert!(breaker.allow_at(s(10)) && !breaker.allow_at(s(10)), "only one probe at a time");
        breaker.record_failure_at(s(10)); // failed probe -> open again for 20s
        assert_eq!(breaker.state_at(s(25)), BreakerState::Open);
        assert!(breaker.allow_at(s(30)));
        breaker.record_success();
        assert_eq!(breaker.state_at(s(30)), BreakerState::Closed);
    }

    #[test]
    fn backoff_is_bounded() {
        let policy = BackoffPolicy {
            max_attempts: 5,
            initial: Duration::from_millis(500),
            max: Duration::from_secs(4),
            multiplier: 2.0,
            jitter: 0.0,
        };
        let delays: Vec<f64> = (1..=5).map(|n| policy.delay(n).as_secs_f64()).collect();
        assert_eq!(delays, vec![0.5, 1.0, 2.0, 4.0, 4.0]);
    }
}
