## ADDED Requirements

### Requirement: Command surface
`setup.cmd` SHALL accept the same options `setup.sh` accepts and behave equivalently:
no arguments installs or resumes, `--status`/`-s` reports without changing anything,
`--reconfigure` re-picks the mouse, `--device ID` sets it non-interactively,
`--uninstall` removes the install, `--yes`/`-y` assumes yes for confirmations, and
`--help`/`-h` prints usage.

#### Scenario: Help
- **WHEN** `setup.cmd --help` is run
- **THEN** usage covering every option and install step is printed and nothing changes

#### Scenario: Unknown option
- **WHEN** an unrecognised option is passed
- **THEN** the script reports it, suggests `--help`, and exits non-zero

#### Scenario: Missing device argument
- **WHEN** `--device` is passed with no value
- **THEN** the script reports the error and exits non-zero

#### Scenario: Status changes nothing
- **WHEN** `setup.cmd --status` is run on any machine state
- **THEN** no file, task, driver or config is created, modified or removed

### Requirement: Idempotent, resumable install
`setup.cmd` SHALL check every step before attempting it, so repeated runs resume
rather than repeat work, including across the reboot the driver install requires.

#### Scenario: Re-run when fully installed
- **WHEN** the script is run again after a successful install
- **THEN** each step reports as already done and nothing is reinstalled

#### Scenario: Re-run after the reboot
- **WHEN** the script is run after rebooting for the driver
- **THEN** it detects the driver is active and continues from mouse selection

#### Scenario: Interrupted install
- **WHEN** a previous run was interrupted partway
- **THEN** the next run completes only the outstanding steps

### Requirement: Driver installation and reboot gate
`setup.cmd` SHALL install the Interception driver, which requires administrator
rights, and SHALL stop with an explanation until the machine has been rebooted,
mirroring the udev-rule-then-re-login gate on Linux.

#### Scenario: Not running as administrator
- **WHEN** the driver must be installed and the script lacks administrator rights
- **THEN** it explains that elevation is required and how to re-run elevated, and
  exits without attempting the install

#### Scenario: Reboot required
- **WHEN** the driver has been installed but not yet activated by a reboot
- **THEN** the script reports that a reboot is required, stops there, and states that
  re-running afterwards continues the install

#### Scenario: Driver install blocked
- **WHEN** the driver install fails because of Secure Boot, driver signature
  enforcement, or interference from anti-cheat software
- **THEN** the script reports the specific cause it detected rather than a generic
  failure

#### Scenario: Driver already present
- **WHEN** a working Interception driver is already installed
- **THEN** the step is reported as done and no reboot is requested

### Requirement: Mouse selection
`setup.cmd` SHALL let the user choose the gated mouse from a list or by physically
moving it, and SHALL record the chosen hardware ID in the config.

#### Scenario: Choose from a list
- **WHEN** the mouse step runs interactively
- **THEN** attached mice are listed with names and the user can select one by number

#### Scenario: Detect by movement
- **WHEN** the user chooses the detect option and moves a mouse
- **THEN** the moved mouse is selected and its hardware ID written to the config

#### Scenario: Non-interactive selection
- **WHEN** `--device ID` is passed
- **THEN** that ID is written to the config without prompting

#### Scenario: Reconfigure
- **WHEN** `--reconfigure` is run on an existing install
- **THEN** the user re-picks the mouse and the service restarts with the new choice

#### Scenario: Assume-yes cannot pick a mouse
- **WHEN** `--yes` is passed and no mouse is configured or supplied via `--device`
- **THEN** the script still requires an explicit choice rather than guessing

### Requirement: Service lifecycle
`setup.cmd` SHALL register the daemon as a Scheduled Task that starts at logon and
restarts on failure, and SHALL manage its install, start, restart and removal.

#### Scenario: Install and start
- **WHEN** the service step runs and no task exists
- **THEN** the task is registered and started, and the script confirms the daemon is
  responding on port 47653

#### Scenario: Stale task definition
- **WHEN** the registered task points at a different script path or interpreter than
  the current repo
- **THEN** the task is re-registered and restarted

#### Scenario: Daemon fails to start
- **WHEN** the daemon does not begin responding within the startup timeout
- **THEN** the script reports the failure and where to find the daemon's log

### Requirement: Configuration management
`setup.cmd` SHALL create the config under `%APPDATA%\onshape-trackball\config` on
first run with every key documented inline, SHALL append keys missing from an older
config, and SHALL detect when the running daemon's settings no longer match the file.

#### Scenario: First run
- **WHEN** no config exists
- **THEN** one is created containing `device`, `left_click_key`, `pan_deadzone_px`,
  `pan_idle_release_ms`, `pan_recenter`, `pan_recenter_margin_px` and
  `pan_yield_to_other_mice`, each with its explanatory comment

#### Scenario: Config predates a setting
- **WHEN** an existing config lacks a key the daemon reads
- **THEN** that key and its documentation are appended, leaving existing values intact

#### Scenario: Config edited but not applied
- **WHEN** the config has been edited since the daemon started
- **THEN** the script notices the drift and restarts the service

### Requirement: Status board
`setup.cmd --status` SHALL report each install step and the live gate state,
interpreting the daemon's status endpoint the way `setup.sh` does.

#### Scenario: Healthy install
- **WHEN** everything is installed and the daemon holds the device
- **THEN** every step shows as done, including driver, task, device grab and config
  freshness

#### Scenario: Extension not loaded
- **WHEN** the daemon reports no extension push has ever arrived
- **THEN** the status board says the extension is not loaded and how to load it

#### Scenario: Daemon not running
- **WHEN** port 47653 does not respond
- **THEN** the status board reports the daemon as down rather than erroring out

### Requirement: Uninstall
`setup.cmd --uninstall` SHALL stop and remove the Scheduled Task and the config, SHALL
ask separately before removing the shared Interception driver and default to keeping
it, and SHALL never modify the repository directory.

#### Scenario: Standard uninstall
- **WHEN** `--uninstall` is confirmed
- **THEN** the task is stopped and removed and the config is deleted

#### Scenario: Driver removal is opt-in
- **WHEN** the uninstall reaches the driver
- **THEN** it asks separately, defaults to keeping it, and notes that removing it
  requires a reboot

#### Scenario: Repo untouched
- **WHEN** any uninstall path completes
- **THEN** no file inside the repository has been modified or deleted

#### Scenario: Extension left to the user
- **WHEN** uninstall completes
- **THEN** it states that the Chrome extension must be removed by hand

### Requirement: Manual extension step
`setup.cmd` SHALL finish by telling the user to load the unpacked extension in Chrome,
naming the repository's `extension` directory, and SHALL report whether the daemon has
ever received a push from it.

#### Scenario: Extension never seen
- **WHEN** the install completes and no extension push has arrived
- **THEN** the script prints the Developer mode and Load unpacked instructions along
  with the absolute path to the extension directory

#### Scenario: Extension already reporting
- **WHEN** the daemon has received a push
- **THEN** the step is reported as done
