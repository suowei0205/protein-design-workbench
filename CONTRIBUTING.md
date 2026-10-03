# Contributing

Use small pull requests with a concrete trigger, resulting behavior, and verification evidence. Keep research data and credentials out of examples and logs. Add a regression test for a behavioral or security fix. Do not change scientific defaults, engine choice, candidate selection, or cache identity as a side effect of monitoring changes.

Install `.[dev]`, run the README checks and `ruff check src tests scripts`, and build a wheel. CI tests Linux on Python 3.10 and 3.12, then checks JS and browser interactions. Tests may explicitly skip unavailable local sockets or Linux ownership on unsupported hosts; CI must exercise Linux/HTTP/kernel tests instead of treating those local skips as validation.

Keep generated runtime state outside the checkout. UI changes must include keyboard, reduced-motion, offline, and narrow-viewport checks. Keep third-party provenance and licenses when updating bundled assets. A fake scientific command test must be clearly labeled and must never be described as a real GPU or MD run.
