"""Live sync against the Google Health API (replaces the deprecated Fitbit Web API).

Two pieces:

* ``auth.py`` -- one-time interactive OAuth authorization (opens a browser,
  catches the redirect on localhost, exchanges the code for a refresh
  token) plus silent refresh for every call after that.
* ``client.py`` -- a thin synchronous wrapper around the four generic
  endpoints (list/reconcile/rollUp/dailyRollUp), since this project stays
  synchronous/pandas-based rather than pulling in an async HTTP stack for
  what is fundamentally a scheduled batch job.
"""
