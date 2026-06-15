def run():
    kw = dict(stdin=None, stdout=None, stderr=None, shell=False)
    # lgtm[py/command-line-injection]
    # nosec B603
    # noqa: E501
    subprocess.Popen(["ls"], **kw)
