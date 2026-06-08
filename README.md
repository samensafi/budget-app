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

There are two ways. Most people want the first.

### The app (easiest)

Go to the [Releases page](https://github.com/samensafi/budget-app/releases/latest),
download `Budget.zip`, unzip it, and move `Budget` to your Applications folder.

The first time you open it, right-click (or Control-click) the Budget app, choose
Open, then Open again. macOS shows this prompt once for any app that is not from
the App Store. After that you just double-click it like any other app.

On first launch Budget downloads everything it needs (the right Python version
and the libraries, into a private folder of its own) and then opens in your
browser. This takes a few minutes the first time and a second or two after that.
It checks for updates and updates itself on each launch.

### From source (for developers)

Clone the repository into an `app` folder and start it:

```
git clone https://github.com/samensafi/budget-app.git budget-app/app
cd budget-app/app
./run.command
```

The first launch sets up a private `userdata` folder next to the code and opens
at http://localhost:8080. The first time you open `run.command` by
double-clicking, macOS may say it is from an unidentified developer. Right-click
the file, choose Open, then Open again. You only have to do that once.

## Running it

Double-click `run.command`, or from a terminal in the `app` folder run
`./run.command`. It opens at http://localhost:8080. To stop it, close the
browser tab and press Ctrl+C in the window that opened.

## Updating

The Budget app updates itself on each launch, and tells you when it has. You can
also check any time from Settings, under Updates, with Check for updates. From
source, run `git pull` in the `app` folder and start it again. Either way updates
only change the code, never your `userdata` folder, so your data stays exactly as
it was.

## Where your data lives

The first time it runs, the app creates a `userdata` folder next to itself for
your transactions, categories and backups. That folder is kept outside the code,
so your data is never part of anything you share. Uploads are the only thing that
leaves your machine: they go to Anthropic to be read and come straight back. Your
API key is stored locally in your own database file.

## Built with

NiceGUI, SQLite, pandas, Plotly and the Anthropic API.
