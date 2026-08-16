"""Antenna pattern acquisition for the USAFA chamber.

Layered so each piece can be used without the ones above it:

    config.py       what a run is - dataclasses only, no I/O
    instruments.py  VNA and positioner - the only module that imports VISA
    engine.py       the step-and-measure loop, reporting through a callback
    writers.py      CSV, Touchstone, metadata, polar plot
    pattern_measure.py  command line front end
"""
