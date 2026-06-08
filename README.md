# Budget

A small budget tracker that runs on your own Mac. All of your data stays on your
computer in a single SQLite file, and the app opens in your web browser.

You can type income and expenses in by hand, or upload a receipt or a statement
and have the transactions pulled out for you. Reading uploads uses Claude and
needs an Anthropic API key. Everything else works without one.

## What it does

- Track income and expenses, each in its own category
- Upload a receipt or statement (PDF, PNG, JPG or TXT) and extract the
  transactions from it automatically
- See where your money goes with monthly charts and a category breakdown
- Search and edit past transactions
- Remember which category a store belongs to, so it fills it in next time
- Export everything to CSV or Excel
- Back up after every change, with a recently deleted list for undoing mistakes

## Requirements

- macOS
- Git. macOS offers to install it the first time it is used.
- An internet connection the first time, to download the app and set it up.
- An Anthropic API key, only if you want uploads read for you. You paste it into
  Settings inside the app.

You do not need to install Python yourself. Setup downloads the exact version
the app needs into its own folder, so it does not matter which Python is or
isn't already on your Mac.

## Installing

Clone the repository into an `app` folder and start it:

```
git clone https://github.com/samensafi/budget-app.git budget-app/app
cd budget-app/app
./run.command
```

The first launch downloads the right Python version and the libraries the app
needs into a private `userdata` folder next to the code, which takes a few
minutes. After that it starts in a second or two and opens at
http://localhost:8080.

The first time you open `run.command` by double-clicking, macOS may say it is
from an unidentified developer. Right-click the file, choose Open, then Open
again. You only have to do that once.

## Running it

Double-click `run.command`, or from a terminal in the `app` folder run
`./run.command`. It opens at http://localhost:8080. To stop it, close the
browser tab and press Ctrl+C in the window that opened.

## Updating

To update, run `git pull` in the `app` folder and start it again. Updates only
change the code, never your `userdata` folder, so your data stays exactly as it
was. If you use the optional launcher app, it pulls updates for you on each
launch.

## Where your data lives

The first time it runs, the app creates a `userdata` folder next to itself for
your transactions, categories and backups. That folder is kept outside the code,
so your data is never part of anything you share. Uploads are the only thing that
leaves your machine: they go to Anthropic to be read and come straight back. Your
API key is stored locally in your own database file.

## Built with

NiceGUI, SQLite, pandas, Plotly and the Anthropic API.
