# SuperFlex Auction Proxy

A Streamlit draft-room assistant for a 12-team, $200 SuperFlex auction. It uses the included **Ringer 2026 SuperFlex rankings** as the only player-value source and can refresh a Google Sheets draft board.

## Run locally

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
streamlit run app.py
```

## Deploy from GitHub

1. Push this folder to a GitHub repository.
2. In Streamlit Community Cloud, choose **Create app**, select the repository, and set the entry point to `app.py`.
3. If the Google Sheet is public/readable by link, no secrets are needed.
4. If it is private, add a Google service-account JSON object to Streamlit secrets:

```toml
[gcp_service_account]
type = "service_account"
project_id = "..."
private_key_id = "..."
private_key = "-----BEGIN PRIVATE KEY-----\\n...\\n-----END PRIVATE KEY-----\\n"
client_email = "..."
client_id = "..."
token_uri = "https://oauth2.googleapis.com/token"
```

Share the spreadsheet with the service-account `client_email`.

## Draft workflow

- Use the **Commissioner console** for natural-language questions such as “Trevor Lawrence is at $20. Should I bid?” It supports close spellings and omitted suffixes, resolves the player, returns only the next legal $1 bid, shows the maximum, and calculates the budget impact.
- Availability questions are checked against every recognized player on the active draft sheet before any bid is recommended. Drafted players return **UNAVAILABLE**.
- Confirm the roster template in **League setup** before relying on full-budget optimization.
- Search and select a nominated player, then enter the current bid. The recommendation always returns only the next legal $1 bid.
- Record wins from the recommendation card or manually in **My roster**.
- The app refreshes the configured Google Sheets tab and can import your roster when you map its player, price, and fantasy-team columns.
- In **Draft Monitor**, confirm the player column once. **Best Available** then removes drafted and avoided players, shows the best overall and by position, and highlights target QBs, eligible top-10 RBs, and the strongest roster fit.
- Edit the sheet URL or tab name in the sidebar and click **Apply draft source**. This clears the prior sheet cache and reloads the new tab immediately.

The bundled values were extracted from the uploaded PDF's dedicated SuperFlex section (updated August 3, 2026). The app does not fetch or invent outside rankings.
