# Lyric

![Windows](https://img.shields.io/badge/platform-Windows-0078D4)
![Python](https://img.shields.io/badge/Python-3.13-3776AB?logo=python&logoColor=white)
![Chrome](https://img.shields.io/badge/Chrome-Manifest_V3-4285F4?logo=googlechrome&logoColor=white)
![Version](https://img.shields.io/badge/version-5.0.3-blue)

A Windows CLI that connects Telegram to Gemini in your regular Chrome browser. Chat, request desktop screenshots, and approve PC actions without an AI API key or local model.

## Setup

Requires Windows, Google Chrome, and Python 3.13 to build.

1. Build the executables:
   ```powershell
   powershell -ExecutionPolicy Bypass -File .\source\build.ps1
   ```
2. Run `Lyric-Setup.exe` and enter your Telegram bot token from [BotFather](https://t.me/BotFather).
3. In `chrome://extensions`, enable **Developer mode**, select **Load unpacked**, and choose `%LOCALAPPDATA%\Lyric\extension`.
4. Open [Gemini](https://gemini.google.com/app), sign in normally, then click **Lyric â†’ Connect this tab**.
5. Start `lyric .` in a new terminal and pair your Telegram account using the displayed code.

Keep Lyric, Chrome, and the connected Gemini tab open. Your normal Chrome session retains your sign-in.

## Commands

| Command | Action |
| --- | --- |
| `lyric .` | Start in the current folder |
| `lyric settings` | Update the bot token and preferences |
| `/screenshot` | Send a desktop screenshot |
| `/new` | Start a fresh Gemini conversation |
| `/cancel` | Cancel pending approvals |
| `/status` | Show connection status |

Basic read-only checks can run automatically. Other commands need **Allow once**; approvals expire after two minutes. Approved scripts use your Windows permissions, so the working folder is not a sandbox. Gemini website limits still apply.

## Development

```powershell
python -m pip install -r source/requirements-dev.txt
python -m pytest source --basetemp .test-temp
```
