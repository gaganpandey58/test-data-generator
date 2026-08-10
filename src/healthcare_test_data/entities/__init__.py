"""Entity-specific generators for source-shaped healthcare test data.

Each submodule owns the fields, nested groups, and scenario mutations for one
business entity.  The shared engine calls their ``generate_record`` function
through a common interface; these modules do not write files or interpret run
configuration themselves.
"""
