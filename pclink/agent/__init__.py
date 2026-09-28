"""Per-session user agent.

The service runs as LocalSystem in session 0 and cannot see a user's foreground
window, idle time, lock state or browser URL.  For each active desktop session
the service launches this agent with the session user's token.  It reports
activity back over a localhost socket and shows a system-tray icon with the
current time-limit state.  It holds no enforcement power of its own: killing it
does not grant extra time, and the service simply relaunches it.
"""
