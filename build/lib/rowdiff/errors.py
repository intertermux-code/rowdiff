class RowdiffError(Exception):
    """Base class for every error rowdiff reports to the user."""


class ConfigError(RowdiffError):
    """Bad DSN, missing primary key, unusable column selection."""


class SourceError(RowdiffError):
    """A database could not be reached or a driver is missing."""
