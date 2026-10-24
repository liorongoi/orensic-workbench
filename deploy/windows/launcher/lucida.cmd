@echo off
rem Lucida command line: batch conversion, verification, backup and restore.
rem Uses this installation's settings (workbench.ini), Python and FFmpeg. Run "lucida --help" for the commands.
"%~dp0python\python.exe" -X utf8 "%~dp0launcher\workbench_launcher.py" cli %*
