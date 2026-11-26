//+------------------------------------------------------------------+
//| JustMarketsBridge.mq5                                             |
//| Links MT5 to the local chart page's bot and executes its orders   |
//| on the account MT5 is logged in to. Attach it to an XAUUSD chart. |
//| A REAL (money) account is refused unless AllowRealAccount = true. |
//+------------------------------------------------------------------+
#property strict
#property version   "1.10"
#property description "Executes the chart page bot's orders. Demo accounts by default; a real account only with AllowRealAccount = true."

#include <Trade\Trade.mqh>

input string ServerUrl = "http://127.0.0.1:8765"; // add exactly this under Tools > Options > Expert Advisors > WebRequest
input long   Magic = 20261029;                    // marks the bot's positions; others are never touched
input int    TimerMilliseconds = 500;
input int    BarsToSend = 1000;
input int    MaxSlippagePoints = 50;
input bool   AllowRealAccount = false;             // REAL MONEY: must be switched on here before a real account can be traded

CTrade          trade;
string          TF_NAMES[4] = {"1m", "5m", "15m", "1h"};
ENUM_TIMEFRAMES TF_PERIODS[4] = {PERIOD_M1, PERIOD_M5, PERIOD_M15, PERIOD_H1};
datetime        bar_open[4];
datetime        bars_sent_at[4];
datetime        last_warning = 0;

bool IsDemo() { return AccountInfoInteger(ACCOUNT_TRADE_MODE) == ACCOUNT_TRADE_MODE_DEMO; }
bool IsReal() { return AccountInfoInteger(ACCOUNT_TRADE_MODE) == ACCOUNT_TRADE_MODE_REAL; }
string Mode() { return IsDemo() ? "DEMO" : IsReal() ? "REAL" : "CONTEST"; }

// Demo always; a real account only when AllowRealAccount is switched on in this add-on's inputs; contest accounts never.
bool MayTrade() { return IsDemo() || (IsReal() && AllowRealAccount); }

int OnInit()
  {
   if(BarsToSend < 50 || BarsToSend > 1500 || TimerMilliseconds < 100)
      return INIT_PARAMETERS_INCORRECT;
   trade.SetExpertMagicNumber(Magic);
   trade.SetDeviationInPoints(MaxSlippagePoints);
   trade.SetTypeFillingBySymbol(_Symbol);
   EventSetMillisecondTimer(TimerMilliseconds);
   Print("JustMarketsBridge started; waiting for MT5 to be logged in to a trade account.");
   return INIT_SUCCEEDED;
  }

void OnDeinit(const int reason) { EventKillTimer(); }

bool account_announced = false;

void OnTimer()
  {
   // The account type is only known once MT5 is logged in and connected; until then just wait.
   if(!TerminalInfoInteger(TERMINAL_CONNECTED) || AccountInfoInteger(ACCOUNT_LOGIN) == 0)
      return;
   if(!MayTrade())
     {
      Alert(IsReal() ? "JustMarketsBridge: this is a REAL money account. Nothing will be traded unless you set AllowRealAccount = true in the add-on's Inputs."
                     : "JustMarketsBridge: contest accounts are never traded.");
      ExpertRemove();
      return;
     }
   if(!account_announced)
     {
      account_announced = true;
      PrintFormat("JustMarketsBridge linked %s on %s account %I64d (%s).%s", _Symbol, Mode(),
                  AccountInfoInteger(ACCOUNT_LOGIN), AccountInfoString(ACCOUNT_SERVER),
                  IsReal() ? " REAL MONEY: orders also need the chart page to be switched to Real account." : "");
     }
   ReportAccount();
   ProcessOrders();
   SendBarsIfDue();
  }

// Trade-server time (often GMT+2/+3) to UTC, rounded to 15 minutes to absorb clock jitter.
long UtcOffsetSeconds() { return (long)MathRound((double)(TimeTradeServer() - TimeGMT()) / 900.0) * 900; }

string Json(string text)
  {
   StringReplace(text, "\\", "\\\\");
   StringReplace(text, "\"", "\\\"");
   return text;
  }

int Http(const string method, const string path, const string body, string &reply)
  {
   char request[];
   char response[];
   string response_headers;
   if(StringLen(body) > 0)
     {
      int length = StringToCharArray(body, request, 0, WHOLE_ARRAY, CP_UTF8);
      ArrayResize(request, length - 1); // drop the terminating zero
     }
   ResetLastError();
   int status = WebRequest(method, ServerUrl + path, "Content-Type: application/json\r\n", 3000, request, response, response_headers);
   if(status == -1 && TimeLocal() - last_warning > 30)
     {
      last_warning = TimeLocal();
      PrintFormat("Cannot reach %s (error %d). Start the dashboard (python main.py) and allow WebRequest for %s in Tools > Options > Expert Advisors.",
                  ServerUrl, GetLastError(), ServerUrl);
     }
   reply = CharArrayToString(response, 0, WHOLE_ARRAY, CP_UTF8);
   return status;
  }

bool IsBotPosition(const ulong ticket)
  {
   return PositionSelectByTicket(ticket) && PositionGetString(POSITION_SYMBOL) == _Symbol && PositionGetInteger(POSITION_MAGIC) == Magic;
  }

int BotPositions()
  {
   int count = 0;
   for(int i = PositionsTotal() - 1; i >= 0; i--)
      if(IsBotPosition(PositionGetTicket(i)))
         count++;
   return count;
  }

// From MT5's deal history: today's account P/L and bot trades since local midnight, and bot wins over 30 days.
void HistoryStats(double &today_pl, int &today_trades, int &wins, int &closed)
  {
   today_pl = 0;
   today_trades = 0;
   wins = 0;
   closed = 0;
   long local_now = (long)TimeLocal();
   long local_midnight = local_now - local_now % 86400;
   datetime today_from = (datetime)(local_midnight + ((long)TimeTradeServer() - local_now)); // in trade-server time
   if(!HistorySelect(TimeTradeServer() - 30 * 86400, TimeTradeServer() + 60))
      return;
   for(int i = HistoryDealsTotal() - 1; i >= 0; i--)
     {
      ulong deal = HistoryDealGetTicket(i);
      long type = HistoryDealGetInteger(deal, DEAL_TYPE);
      if(type != DEAL_TYPE_BUY && type != DEAL_TYPE_SELL)
         continue; // skip deposits and other balance operations
      long entry = HistoryDealGetInteger(deal, DEAL_ENTRY);
      bool bot = HistoryDealGetInteger(deal, DEAL_MAGIC) == Magic && HistoryDealGetString(deal, DEAL_SYMBOL) == _Symbol;
      double result = HistoryDealGetDouble(deal, DEAL_PROFIT) + HistoryDealGetDouble(deal, DEAL_SWAP) + HistoryDealGetDouble(deal, DEAL_COMMISSION);
      if((datetime)HistoryDealGetInteger(deal, DEAL_TIME) >= today_from)
        {
         today_pl += result; // the whole account counts toward the daily loss limit
         if(bot && entry == DEAL_ENTRY_IN)
            today_trades++;
        }
      if(bot && (entry == DEAL_ENTRY_OUT || entry == DEAL_ENTRY_OUT_BY))
        {
         closed++;
         if(result > 0)
            wins++;
        }
     }
  }

void ReportAccount()
  {
   MqlTick tick;
   if(!SymbolInfoTick(_Symbol, tick) || tick.bid <= 0 || tick.ask < tick.bid)
      return;
   double today_pl;
   int today_trades, wins, closed;
   HistoryStats(today_pl, today_trades, wins, closed);
   string positions = "";
   for(int i = PositionsTotal() - 1; i >= 0; i--)
     {
      ulong ticket = PositionGetTicket(i);
      if(!IsBotPosition(ticket))
         continue;
      positions += StringFormat("%s{\"ticket\":%I64u,\"side\":\"%s\",\"volume\":%s,\"price\":%s,\"sl\":%s,\"tp\":%s,\"profit\":%s,\"time\":%I64d}",
                                positions == "" ? "" : ",", ticket,
                                PositionGetInteger(POSITION_TYPE) == POSITION_TYPE_BUY ? "BUY" : "SELL",
                                DoubleToString(PositionGetDouble(POSITION_VOLUME), 2),
                                DoubleToString(PositionGetDouble(POSITION_PRICE_OPEN), _Digits),
                                DoubleToString(PositionGetDouble(POSITION_SL), _Digits),
                                DoubleToString(PositionGetDouble(POSITION_TP), _Digits),
                                DoubleToString(PositionGetDouble(POSITION_PROFIT), 2),
                                (long)PositionGetInteger(POSITION_TIME) - UtcOffsetSeconds());
     }
   string body = StringFormat("{\"login\":%I64d,\"server\":\"%s\",\"company\":\"%s\",\"mode\":\"%s\",\"currency\":\"%s\","
                              "\"balance\":%s,\"equity\":%s,\"symbol\":\"%s\",\"bid\":%s,\"ask\":%s,\"positions\":[%s],"
                              "\"today_pl\":%s,\"today_trades\":%d,\"wins\":%d,\"closed\":%d}",
                              AccountInfoInteger(ACCOUNT_LOGIN), Json(AccountInfoString(ACCOUNT_SERVER)),
                              Json(AccountInfoString(ACCOUNT_COMPANY)), Mode(), Json(AccountInfoString(ACCOUNT_CURRENCY)),
                              DoubleToString(AccountInfoDouble(ACCOUNT_BALANCE), 2), DoubleToString(AccountInfoDouble(ACCOUNT_EQUITY), 2),
                              Json(_Symbol), DoubleToString(tick.bid, _Digits), DoubleToString(tick.ask, _Digits), positions,
                              DoubleToString(today_pl, 2), today_trades, wins, closed);
   string reply;
   Http("POST", "/api/broker/account", body, reply);
  }

// Candles go out right after a new bar opens (so the closed bar is final quickly) and at least every 30 seconds.
void SendBarsIfDue()
  {
   for(int k = 0; k < 4; k++)
     {
      datetime opened = iTime(_Symbol, TF_PERIODS[k], 0);
      if(opened == bar_open[k] && TimeLocal() - bars_sent_at[k] < 30)
         continue;
      MqlRates rates[];
      int n = CopyRates(_Symbol, TF_PERIODS[k], 0, BarsToSend, rates);
      if(n <= 0)
         continue;
      long offset = UtcOffsetSeconds();
      string bars = "";
      for(int i = 0; i < n; i++)
         bars += StringFormat("%s[%I64d,%s,%s,%s,%s]", i == 0 ? "" : ",", (long)rates[i].time - offset,
                              DoubleToString(rates[i].open, _Digits), DoubleToString(rates[i].high, _Digits),
                              DoubleToString(rates[i].low, _Digits), DoubleToString(rates[i].close, _Digits));
      string reply;
      if(Http("POST", "/api/broker/bars", StringFormat("{\"tf\":\"%s\",\"bars\":[%s]}", TF_NAMES[k], bars), reply) == 204)
        {
         bar_open[k] = opened;
         bars_sent_at[k] = TimeLocal();
        }
      return; // one timeframe per timer tick keeps each tick short
     }
  }

void Report(const long id, const bool ok, const ulong ticket, const string message)
  {
   string reply;
   Http("POST", "/api/broker/result",
        StringFormat("{\"id\":%I64d,\"ok\":%s,\"ticket\":%I64u,\"message\":\"%s\"}", id, ok ? "true" : "false", ticket, Json(message)), reply);
   PrintFormat("Order %I64d: %s %s", id, ok ? "done" : "FAILED", message);
  }

double Volume(double lots)
  {
   double step = SymbolInfoDouble(_Symbol, SYMBOL_VOLUME_STEP);
   double minimum = SymbolInfoDouble(_Symbol, SYMBOL_VOLUME_MIN);
   double maximum = SymbolInfoDouble(_Symbol, SYMBOL_VOLUME_MAX);
   lots = MathFloor(lots / step + 1e-9) * step;
   return MathMin(maximum, MathMax(minimum, lots));
  }

// Each line: id|action|side|volume|sl|tp|ticket|sl_distance|tp_distance
void ProcessOrders()
  {
   string reply;
   if(Http("GET", "/api/broker/orders", "", reply) != 200 || StringLen(reply) == 0)
      return;
   string lines[];
   int count = StringSplit(reply, '\n', lines);
   for(int i = 0; i < count; i++)
     {
      string f[];
      if(StringSplit(lines[i], '|', f) != 9)
         continue;
      long id = StringToInteger(f[0]);
      string action = f[1];
      ulong ticket = (ulong)StringToInteger(f[6]);
      if(!MayTrade())
        {
         Report(id, false, 0, "Refused: this account may not be traded (a REAL account needs AllowRealAccount = true)");
         continue;
        }
      if(action == "open")
        {
         if(BotPositions() > 0)
           {
            Report(id, false, 0, "Refused: a bot position is already open");
            continue;
           }
         MqlTick tick;
         if(!SymbolInfoTick(_Symbol, tick))
           {
            Report(id, false, 0, "No price for " + _Symbol);
            continue;
           }
         double min_distance = SymbolInfoInteger(_Symbol, SYMBOL_TRADE_STOPS_LEVEL) * _Point;
         double sl_distance = MathMax(StringToDouble(f[7]), min_distance);
         double tp_distance = MathMax(StringToDouble(f[8]), min_distance);
         double volume = Volume(StringToDouble(f[3]));
         bool sent;
         if(f[2] == "BUY")
            sent = trade.Buy(volume, _Symbol, 0.0, NormalizeDouble(tick.ask - sl_distance, _Digits),
                             NormalizeDouble(tick.ask + tp_distance, _Digits), "paper bot");
         else
            sent = trade.Sell(volume, _Symbol, 0.0, NormalizeDouble(tick.bid + sl_distance, _Digits),
                              NormalizeDouble(tick.bid - tp_distance, _Digits), "paper bot");
         uint code = trade.ResultRetcode();
         bool ok = sent && (code == TRADE_RETCODE_DONE || code == TRADE_RETCODE_PLACED);
         Report(id, ok, trade.ResultOrder(), StringFormat("%s %.2f lot: %s", f[2], volume, trade.ResultRetcodeDescription()));
        }
      else if(action == "modify" || action == "close")
        {
         if(!IsBotPosition(ticket))
           {
            Report(id, false, 0, StringFormat("Position %I64u is not an open bot position", ticket));
            continue;
           }
         bool sent = action == "close"
                     ? trade.PositionClose(ticket)
                     : trade.PositionModify(ticket, NormalizeDouble(StringToDouble(f[4]), _Digits), NormalizeDouble(StringToDouble(f[5]), _Digits));
         Report(id, sent && trade.ResultRetcode() == TRADE_RETCODE_DONE, ticket,
                StringFormat("%s %I64u: %s", action, ticket, trade.ResultRetcodeDescription()));
        }
      else
         Report(id, false, 0, "Unknown action " + action);
     }
  }
//+------------------------------------------------------------------+
