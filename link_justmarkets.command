#!/bin/zsh
# Logs MetaTrader 5 in to a JustMarkets account and starts JustMarketsBridge on an XAUUSD 1-minute chart, using
# MT5's start-up configuration file. The trading password is typed into a macOS dialog on this Mac, written into
# a temporary file for MT5 to read at start-up, and deleted again 30 seconds later.
set -e

PREFIX="$HOME/Library/Application Support/net.metaquotes.wine.metatrader5"
MT5="$PREFIX/drive_c/Program Files/MetaTrader 5"
WINE="/Applications/MetaTrader 5.app/Contents/SharedSupport/wine/bin/wine"
CONFIG="$MT5/link_justmarkets.ini"

if [[ ! -x "$WINE" || ! -f "$MT5/MQL5/Experts/JustMarketsBridge.ex5" ]]; then
  osascript -e 'display alert "Link JustMarkets" message "MetaTrader 5 or the compiled JustMarketsBridge add-on was not found."'
  exit 1
fi

ask() {  # $1 = question, $2 = default answer, $3 = "with hidden answer" for passwords
  osascript -e "text returned of (display dialog \"$1\" default answer \"$2\" $3 with title \"Link JustMarkets\" buttons {\"Cancel\", \"Link\"} default button \"Link\")"
}
LOGIN=$(ask "JustMarkets MT5 account number:" "1300066989")
SERVER=$(ask "MT5 server (shown in your JustMarkets account details):" "JustMarkets-Demo")
PASSWORD=$(ask "MT5 trading password for account $LOGIN (not your email password). It stays on this Mac." "" "with hidden answer")

trap 'rm -f "$CONFIG"' EXIT
cat > "$CONFIG" <<EOF
[Common]
Login=$LOGIN
Password=$PASSWORD
Server=$SERVER
KeepPrivate=1
[Experts]
AllowLiveTrading=1
Enabled=1
WebRequest=1
WebRequestUrl=http://127.0.0.1:8765
[StartUp]
Expert=JustMarketsBridge
Symbol=XAUUSD
Period=M1
EOF
unset PASSWORD

# MT5 only reads a start-up file when it starts, so close it first.
osascript -e 'tell application "MetaTrader 5" to quit' 2>/dev/null || true
for _ in {1..20}; do pgrep -f terminal64.exe >/dev/null || break; sleep 1; done

cd "$MT5"
WINEPREFIX="$PREFIX" "$WINE" "C:\\Program Files\\MetaTrader 5\\terminal64.exe" "/config:C:\\Program Files\\MetaTrader 5\\link_justmarkets.ini" >/dev/null 2>&1 &
sleep 15
rm -f "$CONFIG"  # MT5 read it at start-up; the password file is gone now

# Wait for the result, read by the dashboard from MT5's own logs, and show it.
RESULT="MetaTrader 5 restarted, but it did not log in within 2 minutes. Usually this means MT5 does not know the server yet (it then skips the login silently): in MT5, File → Open an Account → search JustMarkets → click Just Global Markets → Cancel, then run link_justmarkets.command again. Also check that the dashboard runs (python main.py)."
for _ in {1..40}; do
  STATE=$(curl -s -m 3 http://127.0.0.1:8765/api/broker | python3 "$(dirname "$0")/link_status.py" 2>/dev/null || echo WAIT)
  [[ "$STATE" == DONE\|* ]] && { RESULT="${STATE#DONE|}"; break; }
  sleep 3
done
osascript -e "display dialog \"${RESULT//\"/\\\"}\" with title \"Link JustMarkets\" buttons {\"OK\"} default button \"OK\""
echo "$RESULT"
