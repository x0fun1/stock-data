//! CPython-compatible float summation.
//!
//! Python 3.12 changed the builtin `sum()` for floats to Neumaier compensated
//! summation. Every legacy statistic produced on Python 3.13 therefore differs from a
//! naive iterator sum by up to one ulp on some families, which is enough to change a
//! recorded interval endpoint. Use this helper on every float sum that mirrors a
//! Python `sum(...)` call.

/// Neumaier compensated summation, matching CPython 3.12+ `sum()`.
pub fn python_sum<I: IntoIterator<Item = f64>>(values: I) -> f64 {
    let mut total = 0.0f64;
    let mut compensation = 0.0f64;
    for value in values {
        let next = total + value;
        if total.abs() >= value.abs() {
            compensation += (total - next) + value;
        } else {
            compensation += (value - next) + total;
        }
        total = next;
    }
    total + compensation
}

/// Python `math.fsum`: exact (correctly rounded) summation via Shewchuk partials.
///
/// Used where the legacy code calls `math.fsum`; `sum()` differs because it is only
/// compensated (see [`python_sum`]).
pub fn math_fsum<I: IntoIterator<Item = f64>>(values: I) -> f64 {
    let mut partials: Vec<f64> = Vec::new();
    for x in values {
        let mut x = x;
        let mut index = 0usize;
        for position in 0..partials.len() {
            let mut y = partials[position];
            if x.abs() < y.abs() {
                std::mem::swap(&mut x, &mut y);
            }
            let hi = x + y;
            let lo = y - (hi - x);
            if lo != 0.0 {
                partials[index] = lo;
                index += 1;
            }
            x = hi;
        }
        partials.truncate(index);
        partials.push(x);
    }
    // Sum the partials and apply the final rounding correction (CPython `msum`).
    let mut hi = 0.0f64;
    let mut correction: Vec<f64> = Vec::new();
    for position in (0..partials.len()).rev() {
        let x = hi;
        let y = partials[position];
        hi = x + y;
        let lo = y - (hi - x);
        if lo != 0.0 {
            correction.push(lo);
        }
    }
    for position in (0..correction.len()).rev() {
        let x = hi;
        let y = correction[position];
        hi = x + y;
        let lo = x - (hi - y);
        if lo != 0.0 {
            correction[position] = lo;
        }
    }
    hi
}
