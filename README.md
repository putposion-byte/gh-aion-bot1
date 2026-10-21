# GH Aion Shop — Discord bot

Ticket shop bot for the **GH Aion** Discord server: order panel with services and options,
payment choice, private tickets, live price list with `/setprice`, greetings and auto role for new members.

## Files

| File | What it is |
|---|---|
| `bot.py` | the bot |
| `config.json` | services, prices, texts, channel and role IDs |
| `requirements.txt` | Python libraries (discord.py) |
| `data/` | IDs of the panel and price list messages, so the bot edits them instead of posting new ones |
| `railway.json`, `Procfile`, `.python-version` | start settings for hosting |
| `.env.example` | example of the token file. **The real token never goes to GitHub.** |

## Run on a server (Railway)

1. Create a **private** repository on GitHub and upload all files from this folder.
2. On railway.com: **New Project → Deploy from GitHub repo** → pick this repository.
3. Open the service → **Variables** → **New Variable**:
   name `DISCORD_TOKEN`, value — the bot token.
4. Railway starts `python bot.py` automatically (see `railway.json`).
5. Stop the bot on your PC, so only one copy runs.

## Run on a PC

```
pip install -r requirements.txt
python bot.py
```
The token goes into a `.env` file next to `bot.py`: `DISCORD_TOKEN=...`

## Change prices

In Discord: `/setprice` (Manager, CEO and admins only). Everything else is in `config.json`;
after editing it on GitHub, Railway restarts the bot by itself.
