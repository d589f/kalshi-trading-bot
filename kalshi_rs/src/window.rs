//! Window model — `_current_window`, window-open snapshot, tau/elapsed, deltas (§4).
//!
//! KXBTC15M windows are 15 minutes, aligned to :00/:15/:30/:45 UTC. The Kalshi market
//! whose `close_time` == this window's end is the one we trade.

use chrono::{DateTime, Datelike, Timelike, Utc};

use crate::config::WINDOW_MINUTES;

/// `_current_window` — the [start, end) of the current 15-min slot.
pub fn current_window(now: DateTime<Utc>) -> (DateTime<Utc>, DateTime<Utc>) {
    let slot = (now.minute() as i64 / WINDOW_MINUTES) * WINDOW_MINUTES;
    let start = now
        .with_minute(slot as u32)
        .unwrap()
        .with_second(0)
        .unwrap()
        .with_nanosecond(0)
        .unwrap();
    let end = start + chrono::Duration::minutes(WINDOW_MINUTES);
    (start, end)
}

/// Shared window state (the relevant subset of `state.py`).
#[derive(Clone, Debug, Default)]
pub struct WindowState {
    pub window_start: Option<DateTime<Utc>>,
    pub window_end: Option<DateTime<Utc>>,
    pub window_open_price: Option<f64>,
    pub prev_close_price: Option<f64>,
}

impl WindowState {
    /// Detect + handle a new window. Returns true if the window rolled over.
    /// Mirrors the `window_tracker` open-snapshot step (§4.2.2): carry the current
    /// price into prev_close, then set the new open.
    pub fn roll_if_new(&mut self, now: DateTime<Utc>, price: Option<f64>) -> bool {
        let (w_start, w_end) = current_window(now);
        if self.window_start == Some(w_start) {
            return false;
        }
        // new window
        let p = price.or(self.window_open_price);
        if self.window_open_price.is_some() {
            // carry current price into prev_close (price `bn or cl` at the boundary)
            self.prev_close_price = p;
        }
        self.window_start = Some(w_start);
        self.window_end = Some(w_end);
        self.window_open_price = p;
        true
    }

    /// Lazy open/prev seeding inside the per-tick update (`update_delta`, §4.3):
    /// seed open with the first price seen, then prev_close = open if still None.
    pub fn seed_lazy(&mut self, price: Option<f64>) {
        if let Some(px) = price {
            if self.window_open_price.is_none() {
                self.window_open_price = Some(px);
            }
            if self.prev_close_price.is_none() && self.window_open_price.is_some() {
                self.prev_close_price = self.window_open_price;
            }
        }
    }

    /// tau, floored at 0.1, default 2. The LIVE Kalshi-15m paper NORMALIZES by WINDOW_MINUTES
    /// → τ ∈ [0,1] (tasks.py:199: `(w_e-now)/60/WINDOW_MINUTES`, "15m needs ~0.8 not 12").
    /// This differs from the Polymarket spec (raw minutes); the live paper is the source of truth.
    pub fn tau(&self, now: DateTime<Utc>) -> f64 {
        match self.window_end {
            Some(w_end) => {
                let secs = (w_end - now).num_milliseconds() as f64 / 1000.0;
                (secs / 60.0 / WINDOW_MINUTES as f64).max(0.1)
            }
            None => 2.0,
        }
    }

    /// elapsed minutes since window start, default 0 (§4.5).
    pub fn elapsed_min(&self, now: DateTime<Utc>) -> f64 {
        match self.window_start {
            Some(w_start) => (now - w_start).num_milliseconds() as f64 / 1000.0 / 60.0,
            None => 0.0,
        }
    }

    /// delta_from_open = round(price - window_open, 2)
    pub fn delta_from_open(&self, price: f64) -> Option<f64> {
        self.window_open_price.map(|o| round2(price - o))
    }

    /// delta_from_prev = round(price - prev_close, 2)
    pub fn delta_from_prev(&self, price: f64) -> Option<f64> {
        self.prev_close_price.map(|p| round2(price - p))
    }
}

#[inline]
fn round2(x: f64) -> f64 {
    (x * 100.0).round() / 100.0
}

/// Build the KXBTC15M event-time component used in the ticker, for discovery cross-checks.
/// (Kalshi tickers embed the close time in US/Eastern; discovery is done via the API, this
/// is only a convenience for logging.) Returns the UTC window-end formatted as the ticker stem.
pub fn window_end_label(end: DateTime<Utc>) -> String {
    format!(
        "{:04}-{:02}-{:02}T{:02}:{:02}Z",
        end.year(),
        end.month(),
        end.day(),
        end.hour(),
        end.minute()
    )
}
