#property strict
#property version   "1.01"
#property description "Publishes read-only MT5 quotes to the local dashboard."

input string IngestUrl = "http://127.0.0.1:8765/api/quotes";
input int PublishIntervalSeconds = 2;
input int RequestTimeoutMilliseconds = 3000;

int OnInit()
  {
   if(PublishIntervalSeconds < 1 || RequestTimeoutMilliseconds < 1)
     {
      Print("Publish interval and request timeout must be positive.");
      return INIT_PARAMETERS_INCORRECT;
     }

   EventSetTimer(PublishIntervalSeconds);
   Print("Read-only quote bridge started for ", _Symbol);
   return INIT_SUCCEEDED;
  }

void OnDeinit(const int reason)
  {
   EventKillTimer();
  }

void OnTimer()
  {
   MqlTick tick;
   if(!SymbolInfoTick(_Symbol, tick))
     {
      PrintFormat("Could not read the latest tick for %s. Error %d.",
                  _Symbol, GetLastError());
      return;
     }

   if(tick.bid <= 0 || tick.ask < tick.bid || tick.time_msc <= 0)
      return;

   // Tick times are in trade-server time (often GMT+2/+3); the dashboard
   // expects UTC. Round the server offset to 15 minutes to absorb clock jitter.
   long offset_seconds = (long)MathRound((double)(TimeTradeServer() - TimeGMT()) / 900.0) * 900;
   long timestamp_utc_ms = tick.time_msc - offset_seconds * 1000;

   string body = StringFormat(
      "{\"symbol\":\"%s\",\"bid\":%s,\"ask\":%s,\"timestamp_ms\":%I64d}",
      _Symbol,
      DoubleToString(tick.bid, _Digits),
      DoubleToString(tick.ask, _Digits),
      timestamp_utc_ms
   );
   char request[];
   int length = StringToCharArray(body, request, 0, WHOLE_ARRAY, CP_UTF8);
   if(length <= 1)
     {
      Print("Could not encode the quote payload.");
      return;
     }
   ArrayResize(request, length - 1);

   char response[];
   string response_headers;
   ResetLastError();
   int status = WebRequest(
      "POST",
      IngestUrl,
      "Content-Type: application/json\r\n",
      RequestTimeoutMilliseconds,
      request,
      response,
      response_headers
   );
   if(status == 202)
      PrintFormat("Dashboard ignored a stale %s quote. Check the Mac clock or wait for new ticks.", _Symbol);
   else if(status < 200 || status >= 300)
     {
      if(status == -1)
         PrintFormat("Quote publish failed. WebRequest error %d.", GetLastError());
      else
         PrintFormat("Quote publish returned HTTP status %d.", status);
     }
  }
