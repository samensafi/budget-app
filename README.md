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
- Python 3
- An Anthropic API key, only if you want uploads read for you. You paste it into
  Settings inside the app.

## Running it

Double-click `run.command`. The first launch builds a private Python environment
next to the app, which takes a few minutes. After that it starts in a second or
two and opens at http://localhost:8080.

You can also start it from a terminal in this folder:

```
./run.command
```

To stop it, close the browser tab and press Ctrl+C in the window that opened.

## Where your data lives

The first time it runs, the app creates a `userdata` folder next to itself for
your transactions, categories and backups. That folder is kept outside the code,
so your data is never part of anything you share. Uploads are the only thing that
leaves your machine: they go to Anthropic to be read and come straight back. Your
API key is stored locally in your own database file.

## Built with

NiceGUI, SQLite, pandas, Plotly and the Anthropic API.
