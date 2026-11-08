# XBot

Personal build-in-public agent: collects what you actually did each day (journal + public GitHub
commits), finds the one thing worth sharing, drafts 2–3 X posts in your voice, grades them for
truthfulness, and publishes only after you approve.

Status: under construction. See `docs/` for the discovery, architecture and milestone plan, and
`docs/PROGRESS.md` for what works today.

## Quick start (development)

```bash
cp .env.example .env            # then edit XBOT_SECRET_KEY and XBOT_DASHBOARD_PASSWORD
docker compose up -d db         # Postgres 18 on 127.0.0.1:5433 (creates xbot and xbot_test)
cd backend
uv sync
uv run alembic upgrade head
uv run uvicorn xbot.main:create_app --factory --reload
curl http://127.0.0.1:8000/api/health
```

Tests:

```bash
cd backend && uv run pytest
```


## Connecting your X account

XBot posts through the official X API v2 with OAuth 2.0 (Authorization Code with PKCE). It never
scrapes or automates a browser. X charges per request (about $0.015 per post created; a post
containing a link costs about $0.20), so you need a developer account with credits.

1. Go to <https://developer.x.com>, create a **Project** and an **App**.
2. In the app's **User authentication settings**, enable **OAuth 2.0**, choose type **Web App,
   Automated App or Bot**, and set the callback URL to exactly the value of `XBOT_X_REDIRECT_URI`
   (default `http://127.0.0.1:8000/api/x/callback`). Add any website URL X requires.
3. Copy the **OAuth 2.0 Client ID** and **Client Secret** into `.env` as `X_CLIENT_ID` and
   `X_CLIENT_SECRET`, then restart XBot.
4. Buy a small credit pack in the developer console.
5. Run `xbot connect-x` (or open the dashboard's X section), open the printed URL in your browser,
   approve the requested scopes (`tweet.read tweet.write users.read offline.access`), and you will be
   redirected back. `xbot status` and `/api/x/status` then show the connected username.

Tokens are stored encrypted with `XBOT_SECRET_KEY` and refreshed automatically. If X ever rejects
the refresh token, the dashboard shows "reconnect needed" and `xbot connect-x` repeats the flow.
Set `XBOT_PUBLISHER=x` to post for real; `XBOT_PUBLISHER=mock` keeps everything local.


## Choosing the model provider

XBot calls the model through one small interface, so the provider is a setting.

| Provider | `.env` | Notes |
|---|---|---|
| Anthropic (default) | `XBOT_LLM_PROVIDER=anthropic`, `XBOT_LLM_MODEL=claude-opus-5`, `ANTHROPIC_API_KEY=…` | Best quality; about $6–12 a month at one run a day. `claude-sonnet-5` is about $3. |
| OpenRouter | `XBOT_LLM_PROVIDER=openrouter`, `XBOT_LLM_MODEL=google/gemini-3.5-flash-lite`, `OPENROUTER_API_KEY=…` | Any `vendor/model` id with structured-output support. Gemini 3.5 Flash-Lite is about $0.60 a month. Get a key and add credits at <https://openrouter.ai/keys>. |
| Mock | `XBOT_LLM_PROVIDER=mock` | Development only; every output is labelled `[MOCK]`. |

With OpenRouter, XBot sends a strict JSON schema derived from each stage's Pydantic model and
asks OpenRouter to route only to backends that honour it. If a backend rejects strict schemas,
XBot switches once to JSON mode with the schema in the prompt; Pydantic validates every reply
either way. The grounding guards, coverage checks and secret scan are plain code and apply
whatever the model.
