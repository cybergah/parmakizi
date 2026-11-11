# Contributing

The easiest and most useful contribution is **a new site rule**.

## Adding a site

Every site is one line in [`parmakizi/sites.json`](parmakizi/sites.json):

```json
{"name": "Example", "url": "https://example.com/{u}", "probe": "https://api.example.com/users/{u}", "tags": ["dev"], "claimed": "someone-who-exists"}
```

| Field | Required | Meaning |
|---|---|---|
| `name` | yes | Display name, must be unique |
| `url` | yes | Public profile URL shown to the user; `{u}` is the username |
| `claimed` | yes | A username that really exists there, used by `--self-test` |
| `tags` | yes | Categories such as `dev`, `security`, `social`, `video`, `gaming` |
| `probe` | no | URL that is actually requested, usually a public JSON API. Defaults to `url` |
| `detect` | no | `status` (default) or `message` |
| `missing_status` | no | HTTP codes meaning "no such user" (default `[404, 410]`) |
| `missing_text` | no | `message` rule: text that appears only when the user does not exist |
| `missing_body` | no | `message` rule: the *entire* response when the user does not exist, e.g. `null` or `[]` |
| `present_text` | no | `message` rule: text that appears only when the user exists |
| `regex` | no | Usernames the site accepts; others are skipped without a request |

### Rules for rules

- **Public pages and public APIs only.** No logins, cookies, API keys or tokens.
- **Prefer a public JSON API** over scraping HTML: it is faster and breaks less.
- Do not add sites that hide behind bot challenges (Cloudflare "Just a moment…"):
  they give random answers.

### Test it

```bash
pip install -e .
parmakizi --self-test -s "Example"      # must print OK
python -m unittest discover -s tests     # must pass
```

`--self-test` looks up your `claimed` username (must be **found**) and a random
one (must be **not found**). Please paste its output in the pull request.

## Code changes

Keep the single runtime dependency (`httpx`). Tests must stay offline: use
`httpx.MockTransport`, never real network calls.
