

def prune_expired(**kwargs):
    """Единственный путь удаления строк журнала (DRF-1782) — см. ``privacy_audit.retention``."""
    from privacy_audit.retention import prune_expired as _prune_expired

    return _prune_expired(**kwargs)
