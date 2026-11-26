// Trade terminal window: the local Trade terminal page with the JustMarkets web terminal laid over its middle.
// JustMarkets loads as its own page here, not in a frame, so it runs and logs in normally. Needs main.py running.
const { app, BaseWindow, WebContentsView } = require("electron");

const PAGE = "http://127.0.0.1:8765/trade";
const TERMINAL = "https://justmarkets.com/web-trading?account-id=15748669";
const TOP_BAR = 56, BOTTOM_BAR = 48; // heights of the page's top and bottom bars

app.whenReady().then(() => {
  const win = new BaseWindow({ width: 1400, height: 900, title: "Trade terminal" });
  const page = new WebContentsView();
  const terminal = new WebContentsView({ webPreferences: { partition: "persist:justmarkets" } }); // keeps the login between runs
  win.contentView.addChildView(page);
  win.contentView.addChildView(terminal);
  page.webContents.loadURL(PAGE);
  terminal.webContents.loadURL(TERMINAL);
  const layout = () => {
    const { width, height } = win.getContentBounds();
    page.setBounds({ x: 0, y: 0, width, height });
    terminal.setBounds({ x: 0, y: TOP_BAR, width, height: height - TOP_BAR - BOTTOM_BAR });
  };
  layout();
  win.on("resize", layout);
  if (process.env.SNAPSHOT) { // test hook: save a picture of the window, then quit
    setTimeout(async () => {
      require("fs").writeFileSync(process.env.SNAPSHOT, (await terminal.webContents.capturePage()).toPNG());
      console.log("terminal url:", terminal.webContents.getURL(), "title:", terminal.webContents.getTitle());
      app.quit();
    }, 20000);
  }
});

app.on("window-all-closed", () => app.quit());
