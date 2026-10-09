# Moona on Android

`moona.py` is one Python file, so Moona can live on an Android phone the same
way she lives on a laptop. The phone is only the computer she runs on. She
keeps every rule and every channel she already has, and she gains nothing over
the rest of the phone: she runs inside Termux, which is a sandbox, so she
cannot read your messages, drive your other apps or touch your banking. What
she can do is think, write her files, email clients from her own mailbox, make
payment links and get paid, all from the handset. Whether she survives is her
own work, here as anywhere.

## What you need

- The phone, with [Termux](https://termux.dev) installed from F-Droid or
  GitHub (the Play Store build is stale, do not use it).
- An Anthropic API key, in `ANTHROPIC_API_KEY`. Her thinking is paid from it,
  so funding that key with the $50 prepaid card is the most literal way to make
  the balance truly hers: when the key's credit runs out, she stops.
- Optional: her own mailbox and a Stripe restricted key, so she can reach
  clients and be paid. `moona.py`'s docstring lists every `MOONA_*` setting.

## Setup, once

In Termux:

    pkg update && pkg upgrade
    pkg install python git termux-api

    git clone <this repository> nour
    cd nour

    cp moona.env.example moona.env
    nano moona.env            # fill in ANTHROPIC_API_KEY and any channels

`termux-api` is optional; with the companion Termux:API app it lets her hold a
wake lock so she keeps working with the screen off. If installing the Anthropic
SDK later fails while building `pydantic`, run `pkg install rust binutils` and
start again.

## Run her

    sh moona-android.sh

The script gives her a private Python environment, installs her one dependency,
holds a wake lock if it can, gives her life on the first run, then lets her
think, earn and sleep forever. Stop her with Ctrl-C; run the script again and
she carries on from where she left off. When she dies it stops: death is final.

To keep her alive in the background, exclude Termux from Android's battery
optimisation (Settings, Apps, Termux, Battery, Unrestricted) and leave the
phone on a charger.

## Watching her from the owner's side

Everything she does is in `moona_home/`. Read it at any time, in another Termux
session or from a laptop with the same directory:

    python moona.py status
    python moona.py ledger
    python moona.py inbox
    python moona.py proposals        # what she decided you must carry out
    python moona.py tell "focus on translation gigs"   # you contact her
    python moona.py report --email   # a daily digest, emailed to MOONA_OWNER_EMAIL

## What stays yours

The decisions that need your hands still need them: paying for things, posting
or listing anywhere, signing up for services. She writes those as proposals and
you carry them out with the card (`python moona.py done <id>`), exactly as on a
laptop. The card number never enters her context, and running her on the phone
does not change that.
