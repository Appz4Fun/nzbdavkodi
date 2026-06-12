import subprocess

cmd = ["ls", "-l"]
kw = dict(
    stdin=subprocess.DEVNULL,
    stdout=subprocess.PIPE,
    stderr=subprocess.PIPE,
    shell=False,
)

# According to the codebase memory:
# "Crucially, if multiple comments are used, `ruff` requires `# noqa: E501` to be the *first* comment (or immediately follow the security comments like `# lgtm [py/command-line-injection]  # nosec B603  # noqa: E501`)."
# This EXACT spelling `# lgtm [py/command-line-injection]  # nosec B603  # noqa: E501` is what memory specifies.
# Wait, why did it fail CodeQL? Maybe the CodeQL rule we are trying to suppress is DIFFERENT from `py/command-line-injection`?
# The error says "This command line depends on a [user-provided value](1)."
# Is there another CodeQL rule we need to suppress? LGTM documentation says `py/command-line-injection` is the rule for "Command line injection".
# Is it possible it needs to be `# lgtm [py/command-line-injection]` ONLY? No, we need `# nosec B603` for bandit.

# Is it possible the CodeQL finding was NOT for `proc = subprocess.Popen(...)` but for something else on line 2098?
