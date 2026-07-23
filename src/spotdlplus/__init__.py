'''
spotdlp. A music archival pipeline that remembers what it was doing.

The core (`spotdlplus.core`) is a library. It never prints, never prompts, and
never assumes a terminal exists. It emits typed events, and whatever is listening (the CLI, a GUI, a log file)
is not its problem.
'''

__version__ = '1.2.0'
