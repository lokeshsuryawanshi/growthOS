# Deploying Growth OS

Not deployed yet: this needs a hosting account, which only you can create. Everything below is ready to use.

## Rules the deployment must follow
- `APP_ENV=production` (the default). No demo clients, no mock models; the app refuses to start with a weak `JWT_SECRET`.
- **Use a new, empty database.** Never point production at the local `growthos.db` or `demo_mock.db` (they hold fictional demo clients). The Docker/Render config uses `/data/growthos.db` on a persistent disk; migrations create the schema on first start.
- **One process, one worker.** The orchestrator and rate limiter are in-process and SQLite has a single writer.
- Back up the `/data` volume. Losing it loses accounts, drafts and OAuth connections.

## Render (blueprint in `render.yaml`)
1. Push the repo, then New → Blueprint on Render and pick the repo (instance type `starter` or above, for the disk).
2. Fill the `sync: false` variables: `GROQ_API_KEY`, `TAVILY_API_KEY` (free at tavily.com), and `APP_BASE_URL` (the `https://...onrender.com` URL; set it after the first deploy, then redeploy).
3. Optional: `HUNTER_API_KEY`, `GOOGLE_API_KEY`, Gmail variables, `SIGNUP_INVITE_CODE`.
4. Check `https://<url>/healthz`: it must show `"env":"production","fixtures":false`.

## Any Docker host (Fly, Railway, a VM)
```bash
docker build -t growth-os .
docker run -p 8000:8000 -v growthos-data:/data --env-file .env.production growth-os
```
`.env.production` holds the variables from `.env.example` (never `LLM_MODE=mock`).

## Gmail sending
1. Google Cloud Console → APIs & Services: enable the Gmail API, configure the OAuth consent screen (add yourself and testers as test users while the app is unverified), create an OAuth client of type **Web application**.
2. Authorized redirect URI: `<APP_BASE_URL>/oauth/google/callback` (or the exact value you set in `GOOGLE_REDIRECT_URI`; `/auth/gmail/callback` is also served).
3. Set `GOOGLE_OAUTH_CLIENT_ID`, `GOOGLE_OAUTH_CLIENT_SECRET` (`GOOGLE_CLIENT_ID`/`GOOGLE_CLIENT_SECRET` also work) and `GOOGLE_REDIRECT_URI` if you override it. Each founder then clicks Connect Gmail on the Integrations page. Scope is `gmail.send` only.
4. Changing `JWT_SECRET` without setting `ENCRYPTION_KEY` makes stored Gmail tokens unreadable; founders reconnect.

## Smoke test after deploy
Sign up, finish onboarding, open every tab (all show empty states), start research. If a key is missing the run stops with the exact fix on the Activity page.
