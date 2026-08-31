@echo off
setlocal EnableDelayedExpansion
rem ---------------------------------------------------------------------------
rem  Setup / resume installer for the Onshape trackball gate, on Windows.
rem
rem  The Windows half of setup.sh, option for option. Safe to run repeatedly:
rem  every step is checked before it is attempted, so re-running after the
rem  required reboot picks up exactly where it left off.
rem
rem    setup.cmd                 install, prompting where needed
rem    setup.cmd --status        show what is done and what is left, change nothing
rem    setup.cmd --reconfigure   re-pick which mouse is the left one
rem    setup.cmd --device ID     set the left mouse non-interactively
rem    setup.cmd --uninstall     remove the task and config
rem    setup.cmd --yes           assume yes for confirmations
rem
rem  Run setup.cmd --help for the full usage text.
rem ---------------------------------------------------------------------------

set "REPO_DIR=%~dp0"
if "%REPO_DIR:~-1%"=="\" set "REPO_DIR=%REPO_DIR:~0,-1%"

set "TASK_NAME=Onshape trackball gate"
set "PORT=47653"
set "HELPER=%REPO_DIR%\setup_helper.py"
set "GATE=%REPO_DIR%\gate.py"
set "LOG_FILE=%LOCALAPPDATA%\onshape-trackball\gate.log"

set "ASSUME_YES=0"
set "STATUS_ONLY=0"
set "RECONFIGURE=0"
set "UNINSTALL=0"
set "DEVICE_OVERRIDE="

rem ------------------------------------------------------------------ arguments
:parse
if "%~1"=="" goto parsed
if /i "%~1"=="--yes"          ( set "ASSUME_YES=1"   & shift & goto parse )
if /i "%~1"=="-y"             ( set "ASSUME_YES=1"   & shift & goto parse )
if /i "%~1"=="--status"       ( set "STATUS_ONLY=1"  & shift & goto parse )
if /i "%~1"=="-s"             ( set "STATUS_ONLY=1"  & shift & goto parse )
if /i "%~1"=="--reconfigure"  ( set "RECONFIGURE=1"  & shift & goto parse )
if /i "%~1"=="--uninstall"    ( set "UNINSTALL=1"    & shift & goto parse )
if /i "%~1"=="--help"         ( call :usage & exit /b 0 )
if /i "%~1"=="-h"             ( call :usage & exit /b 0 )
rem  The value is read after a goto, not inside the if-block. Batch expands %~1
rem  when it *parses* a block, so a `shift` followed by `%~1` in the same block
rem  reads the pre-shift argument - which made `--device` with no value fall
rem  through instead of erroring.
if /i "%~1"=="--device" ( shift & goto device_value )
echo unknown option: %~1 ^(try --help^) 1>&2
exit /b 2

:device_value
if "%~1"=="" (
  echo --device needs a hardware ID 1>&2
  exit /b 2
)
set "DEVICE_OVERRIDE=%~1"
shift
goto parse

:parsed

call :find_python || exit /b 1

if "%UNINSTALL%"=="1"   ( call :do_uninstall & exit /b !errorlevel! )
if "%STATUS_ONLY%"=="1" ( call :show_status  & exit /b !errorlevel! )

rem ------------------------------------------------------------------ install
echo.
echo Onshape trackball gate - Windows install
echo.

rem  Config first, deliberately. It needs nothing but a writable %APPDATA%, while
rem  the driver step stops the install dead on any machine that has not installed
rem  Interception and rebooted yet - which is every machine, on the first run. Left
rem  in its old place after the driver, the config only ever appeared on the second
rem  run, so there was nothing to read or edit while working through step 1.
call :step_config     || exit /b !errorlevel!
call :step_driver     || exit /b !errorlevel!
call :step_device     || exit /b !errorlevel!
call :step_service    || exit /b !errorlevel!
call :step_grab
call :step_extension

echo.
echo Done. Check it any time with:  setup.cmd --status
exit /b 0


rem ===========================================================================
rem  usage
rem ===========================================================================
:usage
echo Onshape trackball gate
echo.
echo Restricts one mouse so it only works while onshape.com is frontmost in Chrome,
echo and turns its motion into Onshape navigation: move pans ^(Ctrl+right-drag^),
echo right-button + move rotates, wheel zooms, left click clears the selection.
echo.
echo Safe to run repeatedly. Every step is checked before it is attempted, so
echo re-running after the required reboot resumes where it left off.
echo.
echo USAGE
echo   setup.cmd [options]
echo.
echo OPTIONS
echo   -s, --status        Show what is done and what is left; change nothing.
echo       --reconfigure   Re-pick which mouse is the left one.
echo       --device ID     Set the left mouse non-interactively. Takes an
echo                       Interception hardware ID, as printed by --status.
echo       --uninstall     Stop and remove the scheduled task and config. Offers
echo                       to remove the Interception driver too.
echo   -y, --yes           Assume yes for confirmations. Cannot choose a mouse
echo                       for you, and never removes the driver.
echo   -h, --help          This text.
echo.
echo INSTALL STEPS
echo   1. Config        create %%APPDATA%%\onshape-trackball\config, with every
echo                    setting at its default, if it is not there already
echo   2. Driver        install Interception                        ^(administrator^)
echo   3. Reboot        required before the driver takes effect
echo   4. Choose mouse  pick from a list, or 'd' to detect by moving it
echo   5. Service       register and start the scheduled task
echo   6. Mouse grab    confirm the daemon has the device
echo   7. Extension     load the Chrome extension ^(done by hand, in Chrome^)
echo.
echo EXAMPLES
echo   setup.cmd                  Install, or resume an interrupted install.
echo   setup.cmd --status         Check health at a glance.
echo   setup.cmd --reconfigure    Switch to the other mouse.
echo   setup.cmd --uninstall
echo.
echo CONFIGURATION
echo   %%APPDATA%%\onshape-trackball\config, created on first run.
echo   Holds the chosen mouse and pan_idle_release_ms ^(default 150ms^) - how long
echo   a pan stroke stays live after you stop moving. Edit it, then:
echo     schtasks /Run /TN "%TASK_NAME%"
echo.
echo ONCE INSTALLED
echo   curl -s localhost:%PORT%/status
echo   schtasks /Query /TN "%TASK_NAME%"
echo   schtasks /End  /TN "%TASK_NAME%"     Temporarily restore a normal mouse.
goto :eof


rem ===========================================================================
rem  helpers
rem ===========================================================================
:find_python
rem  Deliberately flat. A `for ... do if ... (` block spanning lines is fragile
rem  here and failed outright with "do was unexpected at this time"; there was
rem  never anything to iterate over anyway.
set "PY="
where py.exe >nul 2>&1
if not errorlevel 1 set "PY=py -3"
if defined PY goto :find_python_check
where python.exe >nul 2>&1
if not errorlevel 1 set "PY=python"

:find_python_check
if not defined PY (
  echo   [x] Python 3 not found on PATH.
  echo       Install it from https://python.org ^(tick "Add python.exe to PATH"^),
  echo       or from the Microsoft Store, then run this script again.
  exit /b 1
)
rem The Store's stub prints an advert and exits 9009 rather than running anything.
%PY% -c "import sys" >nul 2>&1
if errorlevel 1 (
  echo   [x] '%PY%' is not a working Python ^(the Microsoft Store stub?^).
  echo       Install a real Python 3, or disable the App execution alias under
  echo       Settings ^> Apps ^> Advanced app settings ^> App execution aliases.
  exit /b 1
)
exit /b 0

:is_admin
net session >nul 2>&1
exit /b %errorlevel%

rem  These go through a variable and delayed expansion rather than echoing %~1
rem  directly. %~1 strips the quotes and cmd then re-parses the result for
rem  redirection, so any <, >, & or | in a message becomes an operator - a driver
rem  error mentioning "vendor/interception/<arch>/" tried to read a file called
rem  "arch" and printed nothing. Delayed expansion is not re-scanned.
:ok
set "_msg=%~1"
echo   [+] !_msg!
goto :eof

:bad
set "_msg=%~1"
echo   [x] !_msg!
goto :eof

:todo
set "_msg=%~1"
echo   [ ] !_msg!
goto :eof

:confirm
rem  %1 prompt   -> errorlevel 0 = yes
if "%ASSUME_YES%"=="1" exit /b 0
call :ask "%~1"
exit /b !errorlevel!

:ask
rem  Always prompts, even under --yes. The driver removal uses this directly:
rem  --yes must never take a kernel driver off the machine on the user's behalf.
rem
rem  Prefers `choice` over `set /p`. set /p reads whatever stdin happens to be and
rem  silently yields an empty string when it is not a console the way it expects -
rem  which is how a typed "y" ended up being read as "no". choice talks to the
rem  console directly and returns the answer as an exit code, so there is no
rem  variable to come back empty. set /p stays as a fallback for a Windows without
rem  choice.exe.
set "_prompt=%~1"
where choice.exe >nul 2>&1 || goto :ask_setp
choice /C YN /N /M "!_prompt! [y/n] "
if errorlevel 2 exit /b 1
if errorlevel 1 exit /b 0
exit /b 1

:ask_setp
set "_reply="
set /p "_reply=!_prompt! [y/N] "
if /i "!_reply!"=="y"   exit /b 0
if /i "!_reply!"=="yes" exit /b 0
exit /b 1

:task_exists
schtasks /Query /TN "%TASK_NAME%" >nul 2>&1
exit /b %errorlevel%


rem ===========================================================================
rem  step 2-3: driver, and the reboot it needs
rem ===========================================================================
:step_driver
echo.
echo Driver
for /f "tokens=1,* delims=|" %%A in ('%PY% "%HELPER%" driver-state 2^>nul') do (
  set "DRIVER_STATE=%%A"
  set "DRIVER_MSG=%%B"
)
if "%DRIVER_STATE%"=="active" (
  call :ok "Interception driver installed and answering"
  exit /b 0
)

if "%DRIVER_STATE%"=="needs_reboot" (
  call :bad "!DRIVER_MSG!"
  echo.
  echo   Reboot, then run this script again - it continues from the next step.
  exit /b 1
)

call :bad "the Interception driver is not installed"
echo.
echo   It is a kernel filter driver, and it is what makes an exclusive grab of one
echo   mouse possible at all. Nothing in user space can do this.
echo.
echo   1. Download the release from https://github.com/oblitum/Interception
echo   2. From an administrator prompt, in its command line folder:
echo          install-interception.exe /install
echo   3. Copy interceptor.dll ^(x64^) next to gate.py:
echo          %REPO_DIR%\interceptor.dll
echo   4. Reboot, then run this script again.
echo.
call :is_admin
if errorlevel 1 (
  echo   Note: you are not running as administrator. The driver install needs it -
  echo   right-click cmd.exe and choose "Run as administrator".
)
call :driver_blockers
exit /b 1

:driver_blockers
rem  A refused driver install is nearly always one of these, and "it failed" is a
rem  useless thing to be told when the fix is specific.
rem  wmic is gone on current Windows 11 builds, so these go through PowerShell.
powershell -NoProfile -Command "$d = Get-CimInstance -Namespace root\Microsoft\Windows\DeviceGuard -ClassName Win32_DeviceGuard -ErrorAction Stop; if ($d.VirtualizationBasedSecurityStatus -ne 0) { exit 0 } else { exit 1 }" >nul 2>&1
if not errorlevel 1 (
  echo   Note: virtualization-based security is on, which blocks some filter drivers.
)
powershell -NoProfile -Command "if ((Confirm-SecureBootUEFI) -eq $true) { exit 0 } else { exit 1 }" >nul 2>&1
if not errorlevel 1 (
  echo   Note: Secure Boot is enabled. Interception is signed, but if the install is
  echo   rejected anyway this is the first thing to check.
)
for %%A in (vgk.sys EasyAntiCheat.sys BEDaisy.sys) do (
  if exist "%SystemRoot%\System32\drivers\%%A" (
    echo   Note: %%A is present. Anti-cheat drivers commonly block, or are blocked
    echo   by, input filter drivers.
  )
)
goto :eof


rem ===========================================================================
rem  step 1: config. Runs before everything else, so the file and its documented
rem  defaults exist even when a later step stops the install.
rem ===========================================================================
:step_config
echo Configuration
for /f "delims=" %%P in ('%PY% "%HELPER%" config-path') do set "CONFIG_FILE=%%P"
for /f "delims=" %%R in ('%PY% "%HELPER%" ensure-config "%DEVICE_OVERRIDE%"') do set "CFG_RESULT=%%R"
if "%CFG_RESULT%"=="created" (
  call :ok "created %CONFIG_FILE%"
) else if "%CFG_RESULT%"=="current" (
  call :ok "%CONFIG_FILE%"
) else (
  call :ok "%CONFIG_FILE% (added missing settings)"
)
exit /b 0


rem ===========================================================================
rem  step 4: which mouse
rem ===========================================================================
:step_device
echo.
echo Mouse
if defined DEVICE_OVERRIDE (
  %PY% "%HELPER%" config-set device "%DEVICE_OVERRIDE%"
  call :ok "set to %DEVICE_OVERRIDE%"
  exit /b 0
)

set "CURRENT="
for /f "delims=" %%D in ('%PY% "%HELPER%" config-get device 2^>nul') do set "CURRENT=%%D"

if defined CURRENT if "%RECONFIGURE%"=="0" (
  set "DEVNAME="
  for /f "delims=" %%N in ('%PY% "%HELPER%" device-name "!CURRENT!"') do set "DEVNAME=%%N"
  call :ok "!DEVNAME!"
  exit /b 0
)

call :pick_device
exit /b %errorlevel%

:pick_device
echo.
echo   Attached mice:
set "COUNT=0"
for /f "tokens=1,2,3 delims=|" %%A in ('%PY% "%HELPER%" list-mice 2^>nul') do (
  echo      %%A^) %%C
  set "HWID_%%A=%%B"
  set "COUNT=%%A"
)
if "%COUNT%"=="0" (
  call :bad "no mice found. Is the driver active?"
  exit /b 1
)
echo      d^) detect - press d, then MOVE the mouse you want gated
echo.
rem  --yes must not silently pick a mouse: choosing the wrong one makes the user's
rem  normal mouse go dead, which is not a default anyone would want guessed.
rem
rem  Read through `choice` for the same reason the y/N prompts are: a set /p that
rem  comes back empty here means the install cannot proceed at all. choice takes
rem  one keypress from the console, so there is no variable to come back empty.
rem  It accepts only single characters, so more than nine mice falls back to set /p.
set "CHOICE="
set "OPTS="
for /L %%i in (1,1,%COUNT%) do set "OPTS=!OPTS!%%i"
set "OPTS=!OPTS!D"
if %COUNT% GEQ 10 goto :pick_setp
where choice.exe >nul 2>&1 || goto :pick_setp
choice /C !OPTS! /N /M "  Which mouse? "
set "_sel=!errorlevel!"
if !_sel! EQU 0 (
  call :bad "nothing chosen"
  exit /b 1
)
if !_sel! GTR %COUNT% ( set "CHOICE=d" ) else ( set "CHOICE=!_sel!" )
goto :pick_have

:pick_setp
set /p "CHOICE=  Which mouse? "

:pick_have
if /i "!CHOICE!"=="d" (
  echo   Move the mouse you want gated...
  set "DETECTED="
  for /f "delims=" %%H in ('%PY% "%HELPER%" detect-mouse 10') do set "DETECTED=%%H"
  if not defined DETECTED (
    call :bad "no movement detected"
    exit /b 1
  )
  %PY% "%HELPER%" config-set device "!DETECTED!"
  set "DEVNAME="
  for /f "delims=" %%N in ('%PY% "%HELPER%" device-name "!DETECTED!"') do set "DEVNAME=%%N"
  call :ok "chose !DEVNAME!"
  exit /b 0
)

if not defined CHOICE (
  call :bad "nothing chosen"
  exit /b 1
)
set "PICKED=!HWID_%CHOICE%!"
if not defined PICKED (
  call :bad "'!CHOICE!' is not one of the options"
  exit /b 1
)
%PY% "%HELPER%" config-set device "!PICKED!"
set "DEVNAME="
for /f "delims=" %%N in ('%PY% "%HELPER%" device-name "!PICKED!"') do set "DEVNAME=%%N"
call :ok "chose !DEVNAME!"
exit /b 0


rem ===========================================================================
rem  step 5: the scheduled task
rem ===========================================================================
:step_service
echo.
echo Service
call :expected_command
call :task_exists
if errorlevel 1 (
  call :register_task || exit /b 1
) else (
  call :task_command_matches
  if errorlevel 1 (
    call :todo "task points somewhere else; re-registering"
    schtasks /Delete /TN "%TASK_NAME%" /F >nul 2>&1
    call :register_task || exit /b 1
  )
)

rem  Config edited since the daemon started? Restart rather than leave it stale.
%PY% "%HELPER%" status >nul 2>&1
if not errorlevel 1 (
  %PY% "%HELPER%" drift >nul 2>&1
  if errorlevel 1 (
    call :todo "config changed since the daemon started; restarting"
    schtasks /End /TN "%TASK_NAME%" >nul 2>&1
    schtasks /Run /TN "%TASK_NAME%" >nul 2>&1
  )
) else (
  schtasks /Run /TN "%TASK_NAME%" >nul 2>&1
)

%PY% "%HELPER%" wait-daemon 20 >nul 2>&1
if errorlevel 1 (
  call :bad "the daemon did not start responding on port %PORT%"
  echo       Log: %LOG_FILE%
  echo       Try it in the foreground to see why:
  echo         %PY% "%GATE%"
  exit /b 1
)
call :ok "running, responding on port %PORT%"
exit /b 0

:expected_command
rem  The task runs a generated launcher rather than a command line of its own.
rem  schtasks /TR needs every inner quote escaped, and the command here has three
rem  paths and a redirect in it - putting that in a .cmd keeps the registration to
rem  one quoted path, and leaves the user something they can run by hand to see
rem  what the service actually does.
set "STATE_DIR=%LOCALAPPDATA%\onshape-trackball"
set "LAUNCHER=%STATE_DIR%\run-gate.cmd"
rem  The real pythonw.exe, asked of Python itself - deliberately not the `pyw`
rem  launcher. `pyw` spawns pythonw and returns immediately, so the wrapper exits,
rem  the task completes, and the daemon is left orphaned with no task tree. That
rem  makes `schtasks /End` a no-op, which is the documented way to get the gated
rem  mouse back. Invoked directly, cmd waits for it and the task owns the process.
set "PYW="
for /f "delims=" %%P in ('%PY% -c "import os,sys;print(os.path.join(os.path.dirname(sys.executable),'pythonw.exe'))" 2^>nul') do set "PYW=%%P"
if not defined PYW set "PYW=%PY%"
if not exist "%PYW%" set "PYW=%PY%"
goto :eof

:write_launcher
if not exist "%STATE_DIR%" mkdir "%STATE_DIR%" >nul 2>&1
rem  pythonw, so a logon does not flash a console window; output is teed to a log
rem  the status board can point at.
 > "%LAUNCHER%" echo @echo off
>> "%LAUNCHER%" echo rem Generated by setup.cmd. Re-run it to regenerate.
>> "%LAUNCHER%" echo rem Quoted and called directly so cmd waits for it: the task must
>> "%LAUNCHER%" echo rem keep owning the daemon, or schtasks /End cannot stop it.
>> "%LAUNCHER%" echo "%PYW%" "%GATE%" ^>^> "%LOG_FILE%" 2^>^&1
goto :eof

:register_task
rem  Any launcher from an older install is dead weight now, and leaving it around
rem  invites someone to run the stale copy by hand.
if exist "%LAUNCHER%" del /q "%LAUNCHER%" >nul 2>&1
rem  At logon, only while this user is logged on: the daemon injects input and reads
rem  the foreground window, and neither works from session 0. That is also why this
rem  is a scheduled task rather than a Windows service.
rem
rem  Registered through PowerShell rather than `schtasks /Create /SC ONLOGON`, which
rem  fails with "Access is denied" for a standard user: that trigger can target any
rem  user, so schtasks demands administrator rights for it. Register-ScheduledTask
rem  creates the same at-logon task for the current user without elevation, which is
rem  what makes this the real counterpart of a systemd *user* unit - only the driver
rem  step needs admin. It also carries the settings in one call, where schtasks
rem  cannot express restart-on-failure at all.
rem  Paths travel in the environment, not inline: the action needs the script path
rem  quoted inside a PowerShell string inside a batch string, and [char]34 keeps
rem  that nesting out of the batch parser entirely.
set "OSGATE=%GATE%"
set "OSPYW=%PYW%"
set "PSREG=$a = New-ScheduledTaskAction -Execute $env:OSPYW -Argument ([char]34 + $env:OSGATE + [char]34);"
set "PSREG=!PSREG! $t = New-ScheduledTaskTrigger -AtLogOn -User '%USERDOMAIN%\%USERNAME%';"
set "PSREG=!PSREG! $s = New-ScheduledTaskSettingsSet -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1) -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -ExecutionTimeLimit ([TimeSpan]::Zero);"
set "PSREG=!PSREG! Register-ScheduledTask -TaskName '%TASK_NAME%' -Action $a -Trigger $t -Settings $s -Force -ErrorAction Stop | Out-Null"
set "TASKERR="
for /f "delims=" %%E in ('powershell -NoProfile -ExecutionPolicy Bypass -Command "try { !PSREG!; exit 0 } catch { Write-Output $_.Exception.Message; exit 1 }" 2^>^&1') do set "TASKERR=%%E"
if defined TASKERR (
  rem  Printed, not swallowed. Hiding this behind >nul is what turned a one-line
  rem  "Access is denied" into an unexplained "could not register".
  call :bad "could not register the scheduled task"
  call :bad "!TASKERR!"
  exit /b 1
)
call :ok "registered the scheduled task"
schtasks /Run /TN "%TASK_NAME%" >nul 2>&1
exit /b 0

:task_command_matches
rem  A task pointing at a different clone or interpreter is worse than no task: it
rem  runs, so everything looks installed, but it is not this code that is running.
rem  The task runs the interpreter directly, so its XML names both halves: the
rem  pythonw.exe it will run and this repo's gate.py as the argument. Both are
rem  checked, so a task left over from another clone or another Python is replaced
rem  rather than trusted.
schtasks /Query /TN "%TASK_NAME%" /XML 2>nul | find /i "%GATE%" >nul 2>&1
if errorlevel 1 exit /b 1
schtasks /Query /TN "%TASK_NAME%" /XML 2>nul | find /i "%PYW%" >nul 2>&1
exit /b %errorlevel%


rem ===========================================================================
rem  step 6: is the device actually grabbed
rem ===========================================================================
:step_grab
echo.
echo Mouse grab
set "ATTACHED="
for /f "delims=" %%A in ('%PY% "%HELPER%" status device_attached 2^>nul') do set "ATTACHED=%%A"
if /i "%ATTACHED%"=="True" (
  call :ok "the daemon has the device"
) else (
  call :bad "the daemon is running but does not have the device"
  echo       Is the mouse plugged in? Is the configured hardware ID still right?
  echo       setup.cmd --reconfigure re-picks it.
)
goto :eof


rem ===========================================================================
rem  step 7: the extension, which is loaded by hand
rem ===========================================================================
:step_extension
echo.
echo Extension
set "PUSH="
for /f "delims=" %%P in ('%PY% "%HELPER%" status seconds_since_extension_push 2^>nul') do set "PUSH=%%P"
if defined PUSH (
  call :ok "the extension is reporting"
  goto :eof
)
call :todo "not loaded yet - Chrome cannot be scripted into this, so:"
echo.
echo         1. Open  chrome://extensions
echo         2. Turn on  Developer mode  ^(top right^)
echo         3. Load unpacked, and choose:
echo               %REPO_DIR%\extension
echo.
echo      Chrome will nag about developer-mode extensions on startup. That is
echo      unavoidable for unpacked extensions.
goto :eof


rem ===========================================================================
rem  --status
rem ===========================================================================
:show_status
echo.
echo Onshape trackball gate - status
echo.

echo Driver
for /f "tokens=1,* delims=|" %%A in ('%PY% "%HELPER%" driver-state 2^>nul') do (
  set "DRIVER_STATE=%%A"
  set "DRIVER_MSG=%%B"
)
if "%DRIVER_STATE%"=="active" (
  call :ok "installed and answering"
) else (
  call :bad "!DRIVER_MSG!"
)

echo.
echo Configuration
for /f "delims=" %%P in ('%PY% "%HELPER%" config-path') do set "CONFIG_FILE=%%P"
if exist "%CONFIG_FILE%" (
  call :ok "%CONFIG_FILE%"
) else (
  call :todo "not created yet"
)
set "CURRENT="
for /f "delims=" %%D in ('%PY% "%HELPER%" config-get device 2^>nul') do set "CURRENT=%%D"
if defined CURRENT (
  set "DEVNAME="
  for /f "delims=" %%N in ('%PY% "%HELPER%" device-name "!CURRENT!"') do set "DEVNAME=%%N"
  call :ok "mouse: !DEVNAME!"
  echo         !CURRENT!
) else (
  call :todo "no mouse chosen  (setup.cmd --reconfigure)"
)

echo.
echo Service
call :task_exists
if errorlevel 1 (
  call :todo "scheduled task not registered"
) else (
  call :ok "scheduled task registered"
)

%PY% "%HELPER%" status >nul 2>&1
if errorlevel 1 (
  call :bad "the daemon is not responding on port %PORT%"
  echo         Nothing is grabbing the mouse, so it behaves normally.
  echo         Log: %LOG_FILE%
  goto :status_end
)
call :ok "daemon responding on port %PORT%"

%PY% "%HELPER%" drift >nul 2>&1
if errorlevel 1 (
  call :bad "daemon settings do NOT match the config  (run setup.cmd)"
) else (
  call :ok "daemon settings match the config"
)

echo.
echo Gate
call :status_field device_attached "the daemon has the device" "the daemon does not have the device"
call :status_field chrome_focused  "Chrome is frontmost"        "Chrome is not frontmost"
call :status_field onshape_tab     "the active tab is Onshape"  "the active tab is not Onshape"
call :status_field gate_open       "GATE OPEN - the mouse navigates Onshape" "gate closed - the mouse is inert"

set "PUSH="
for /f "delims=" %%P in ('%PY% "%HELPER%" status seconds_since_extension_push 2^>nul') do set "PUSH=%%P"
if defined PUSH (
  call :ok "extension last reported !PUSH!s ago"
) else (
  call :bad "the extension has never reported - it is not loaded"
  echo         chrome://extensions ^> Developer mode ^> Load unpacked ^>
  echo         %REPO_DIR%\extension
)

rem  The background worker and the content script fail independently, and only the
rem  content script measures the 3D view. Without it the daemon falls back to the
rem  whole Chrome window, so the cursor can wander onto the feature tree - which
rem  looks like a panning bug rather than a missing script.
if defined PUSH (
  set "CANVAS="
  for /f "delims=" %%C in ('%PY% "%HELPER%" status canvas_rect 2^>nul') do set "CANVAS=%%C"
  if defined CANVAS (
    call :ok "reporting the 3D view region"
  ) else (
    call :bad "no 3D view region reported"
    echo         The cursor is penned to the whole Chrome window, so it can stray
    echo         onto the feature tree. Reload the Onshape tab: Chrome does not
    echo         inject content scripts into pages that were already open when the
    echo         extension was loaded.
  )
)

:status_end
echo.
exit /b 0

:status_field
set "_V="
for /f "delims=" %%V in ('%PY% "%HELPER%" status %~1 2^>nul') do set "_V=%%V"
if /i "!_V!"=="True" ( call :ok "%~2" ) else ( call :bad "%~3" )
goto :eof


rem ===========================================================================
rem  --uninstall
rem ===========================================================================
:do_uninstall
echo.
echo Uninstall
echo.
call :confirm "  Remove the scheduled task and configuration?"
if errorlevel 1 (
  echo   Nothing done.
  exit /b 0
)

call :expected_command
call :task_exists
if errorlevel 1 (
  call :todo "no scheduled task to remove"
) else (
  schtasks /End    /TN "%TASK_NAME%" >nul 2>&1
  schtasks /Delete /TN "%TASK_NAME%" /F >nul 2>&1
  call :ok "removed the scheduled task"
)

if exist "%LAUNCHER%" (
  del /q "%LAUNCHER%" >nul 2>&1
  call :ok "removed %LAUNCHER%"
)

for /f "delims=" %%P in ('%PY% "%HELPER%" config-path') do set "CONFIG_FILE=%%P"
if exist "%CONFIG_FILE%" (
  del /q "%CONFIG_FILE%" >nul 2>&1
  call :ok "removed %CONFIG_FILE%"
) else (
  call :todo "no config to remove"
)

echo.
rem  The driver is shared state: other software may rely on it, and removing it
rem  costs a reboot. Asked separately, and defaulting to no, exactly as setup.sh
rem  treats the udev rule and the 'input' group.
echo   The Interception driver is shared with anything else that uses it, and
echo   removing it needs another reboot.
call :ask "  Remove the Interception driver too?"
if not errorlevel 1 (
  echo   Run this from an administrator prompt, in the driver's folder:
  echo       install-interception.exe /uninstall
  echo   then reboot.
) else (
  call :ok "kept the Interception driver"
)

echo.
echo   The Chrome extension has to be removed by hand: chrome://extensions.
echo   Nothing in %REPO_DIR% was touched.
exit /b 0
