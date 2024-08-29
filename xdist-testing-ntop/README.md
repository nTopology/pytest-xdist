# Testing pytest-xdist Scheduler Custumization
- Run with `python -m pytest test.py` to run tests
- Run with `python -m pytest test.py -n <worker count> --dist customgroup --junit-xml results.xml -v` to use new scheduler + report to xml and have verbose terminal output
    - Verbose terminal output is semi-required when using customgroup. It allows the user to confirm the correct tests are running with the correct number of processes.

## Notes:
- Install local pytest with `python -m pip install .`
- Can set breakpoints, default scheduler is `load.py` and breakpoint in the init method are hit when running tests with `-n`
- 