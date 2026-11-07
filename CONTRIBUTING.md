# Contributing

Thanks for helping. timelens stays small on purpose: a few Python files, the standard library plus `pyobjc`, no server.

- **Before a pull request:** `timelens selftest` (or `bin/timelens selftest` from a checkout). If you change how the reports or the setup page look, run `./docs/render.sh` and commit the new images.
- **New goal presets** go in `presets.toml`. Describe what each category looks like on screen, keep categories from overlapping, and end with the `other` catch-all.
- **Report behaviour** goes in plain code, not in the model prompt. The model only labels minutes.
- **New model providers:** if it speaks the System One / Decisions format, it should only need config changes (and an entry in `MODELS` in `onboarding.py` to appear in setup). Run `eval.py` on your own screen and share the numbers.
- **Privacy is a feature.** Don't add anything that stores screenshots or sends more than the one request per minute.
- **Porting to Linux or Windows:** the platform layer is `front_window`, `away`, `browser_url` and `screenshot` in `timelens.py`.
