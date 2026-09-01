# Project Snow v0.5.0 Desktop Preview Client

This is a thin Electron wrapper around the existing local web application. It
does not contain `Data/`, model credentials, or a second dialogue implementation.
The BrowserWindow keeps Node integration disabled, context isolation and the
sandbox enabled. Only microphone access requested by the exact local client
origin is permitted; filesystem, geolocation, notifications and other browser
permissions remain denied.

## Prerequisites

Start the API and web server from two PowerShell windows:

```powershell
cd C:\Users\25685\Desktop\Myprojects\Project_Snow\App
python -m backend.snow_app.main
```

```powershell
cd C:\Users\25685\Desktop\Myprojects\Project_Snow\App
python scripts/dev_server.py
```

## Development client

```powershell
cd C:\Users\25685\Desktop\Myprojects\Project_Snow\App\client
npm install
npm start
```

The client always opens the experience selector at `http://127.0.0.1:8080/`.
The formal chat surface is available at `/immersive/`; the evidence, review and
feedback workspace remains available at
`http://127.0.0.1:8080/workspace/`. If either local service is unavailable, the
client shows the startup commands and a retry button.

## Windows portable build

Run the desktop and narrow-window smoke test while both local services are up:

```powershell
cd C:\Users\25685\Desktop\Myprojects\Project_Snow\App\client
npm run smoke
```

The test checks the selector, the immersive route, the 390-pixel composer hit target,
text portraits and `/workspace/`, then writes its screenshot to
`App/runtime/screenshots/electron-mobile.png`.

Build the portable executable:

```powershell
cd C:\Users\25685\Desktop\Myprojects\Project_Snow\App\client
npm run package:win
```

The portable executable is written below `App/client/dist/`. It still expects
the API and web service to be running on the same machine.
