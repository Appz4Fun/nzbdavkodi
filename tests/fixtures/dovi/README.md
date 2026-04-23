# Vendored Dolby Vision RPU fixtures

The three binary files in this directory are vendored byte-for-byte from
[`quietvoid/dovi_tool`][upstream]:

| File | Upstream path | Purpose |
|---|---|---|
| `fel_orig.bin` | `assets/tests/fel_orig.bin` | Profile 7 FEL (Full Enhancement Layer) reference RPU |
| `mel_orig.bin` | `assets/tests/mel_orig.bin` | Profile 7 MEL (Minimal Enhancement Layer) reference RPU |
| `profile8.bin` | `assets/tests/profile8.bin` | Profile 8 (single-layer) reference RPU |

[upstream]: https://github.com/quietvoid/dovi_tool

**Pinned upstream commit (last re-verified):**
`e7bef8d979a3a975a5eb6930c25e07e554cecee9` (2026-04-23).

Git-blob SHA1s match upstream exactly — verify with `git hash-object`
against `gh api repos/quietvoid/dovi_tool/contents/assets/tests/<file>.bin
--jq .sha` when refreshing. Fixtures are frozen; any content change in this
directory means either upstream rewrote them (re-verify against the new
upstream commit and update this README) or something is corrupt.

## Cross-validation

`tests/test_dv_rpu.py` asserts `parse_rpu_payload` output matches the
upstream semantics. To re-verify manually after a parser change, install
`dovi_tool` and compare:

```bash
brew install dovi_tool        # macOS, or `cargo install dovi_tool`
dovi_tool info --frame 0 tests/fixtures/dovi/mel_orig.bin
# Should report dovi_profile=7, el_type=MEL — matches our parser output.
```

## License

`dovi_tool` is MIT-licensed. The license text below is reproduced verbatim
from [the upstream repository's LICENSE file][license]; it applies to the
fixtures in this directory.

[license]: https://github.com/quietvoid/dovi_tool/blob/main/LICENSE

```
MIT License

Copyright (c) 2021 quietvoid

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```
