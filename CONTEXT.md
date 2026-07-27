# Domain context

## Check-in workflow

The command-line workflow configures credentials, notification choices and a
daily schedule. The worker reads that configuration, obtains the official
CodeBuddy/WorkBuddy login state and performs the remote check-in.

## Platform adapter

The platform adapter owns operating-system behaviour. `MacOSPlatform` and
`WindowsPlatform` are the only adapters at this seam. They contain scheduling,
CodeBuddy login process control, executable and data paths, local application
launching, desktop notifications and cleanup rules.

The command workflow and worker must not branch on operating-system behaviour;
they select an adapter once and use its interface. Platform-independent check-in,
WeChat and configuration behaviour remains outside the adapters.

## Schedule

A schedule means both the daily run and the login catch-up run. On macOS it is
represented by a LaunchAgent plist; on Windows it is represented by two Task
Scheduler entries. The platform adapter preserves this invariant behind one
schedule interface.
