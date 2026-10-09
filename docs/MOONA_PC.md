# Moona on a PC

`moona.py` is one Python file, so Moona runs on any Windows, macOS or Linux PC.
She keeps every rule and every channel she has anywhere else. The PC is only the
computer she runs on.

## 1. Get the files onto the PC

With git:

    git clone -b ccr-a23dd1ed-g24sv1 https://github.com/mohdaboufoul-lgtm/nour.git
    cd nour

No git, or a private repo: download the three files `moona.py`, `moona.env.example`
and, for your platform, `moona.ps1` (Windows) or `moona-android.sh` (macOS/Linux),
into one folder and open a terminal in it.

## 2. Install Python

Check first:

    python --version      # or python3 --version

If it is missing, get Python 3.11 or newer from https://python.org. On the Windows
installer, tick **Add Python to PATH**. On macOS, `brew install python`; on Debian
or Ubuntu, `sudo apt install python3-venv`.

## 3. Give her the key

    copy moona.env.example moona.env     # Windows
    cp moona.env.example moona.env       # macOS / Linux

Open `moona.env`, paste your key on the `ANTHROPIC_API_KEY=` line, and save.
Funding that key with the $50 prepaid card is what makes the balance hers: when
its credit runs out, she stops. The same file holds her optional channels
(mailbox, Stripe, bank details); `moona.py`'s docstring documents them all.

## 4. Start her

Windows (PowerShell):

    powershell -ExecutionPolicy Bypass -File .\moona.ps1

macOS or Linux:

    sh moona-android.sh

Either launcher builds her a private Python environment, installs her one
dependency, gives her life on the first run, then lets her think, earn and sleep
forever. Stop her with Ctrl-C; run the launcher again and she resumes from where
she left off. When she dies it stops: death is final.

To keep her alive, leave the terminal open and the PC awake (disable sleep, or
keep it plugged in). Everything she does is written under `moona_home/`.

## 5. Watch her, from the owner's side

In another terminal in the same folder (use `python` or the launcher's
`.moona-venv` Python):

    python moona.py status           # alive, balance, what she decided you must carry out
    python moona.py proposals        # her decisions, word for word
    python moona.py done 1 --spent 12.50 --note "paid with the card"
    python moona.py refuse 2 --note "could not"
    python moona.py paid 40 --note "client X"     # a bank transfer landed
    python moona.py ledger | journal | inbox | mail | links | payments
    python moona.py tell "focus on translation gigs"   # you contact her; she reads it next session
    python moona.py report               # a digest of her state and last 24h
    python moona.py report --email       # the same, emailed to MOONA_OWNER_EMAIL

She reports to you herself: her prompt asks her for a short daily report with
`message_owner`, which lands in her inbox and, when you set `MOONA_OWNER_EMAIL` and
her mailbox, is emailed to you. To guarantee a daily report regardless of her, schedule
`moona.py report --email`: on Windows with Task Scheduler (a daily trigger running
`powershell -File ...\.moona-venv\Scripts\python.exe moona.py report --email`), on
macOS or Linux with cron (`0 18 * * * cd /path/to/nour && .moona-venv/bin/python moona.py report --email`).

Payments through her Stripe links book themselves. Only bank transfers need your
`paid` command. The decisions that need your hands (paying for things, posting or
listing anywhere, signing up for services) stay yours: she writes them as
proposals and you carry them out with the card. The card number never enters her
context, on a PC no more than anywhere else.
