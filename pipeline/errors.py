class PipelineError(Exception):
    """A failure that must stop the run before anything is published."""
