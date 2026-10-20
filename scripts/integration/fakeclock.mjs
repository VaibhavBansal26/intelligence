// Test-harness clock shifter, preloaded into the Node apps with `node --import fakeclock.mjs`.
// It makes `new Date()` / `Date.now()` return real time plus an offset (milliseconds) read from the file named by
// FAKE_CLOCK_FILE, so the traffic generator can drive the apps' own PUBLIC HTTP APIs "as if" it were days ago and
// build history without ever touching their databases. Timers and performance.now() stay on real time.
// Not used for the rate-limit / auth phases of the run (the apps are restarted without it).
import fs from 'node:fs';

const file = process.env.FAKE_CLOCK_FILE;
if (file) {
  const RealDate = Date;
  let offset = 0;
  let checkedAt = 0;
  const current = () => {
    const real = RealDate.now();
    if (real - checkedAt > 3) {
      checkedAt = real;
      try {
        offset = Number(fs.readFileSync(file, 'utf8').trim()) || 0;
      } catch {
        offset = 0;
      }
    }
    return real + offset;
  };
  class FakeDate extends RealDate {
    constructor(...args) {
      if (args.length === 0) super(current());
      else super(...args);
    }
    static now() {
      return current();
    }
  }
  globalThis.Date = FakeDate;
}
