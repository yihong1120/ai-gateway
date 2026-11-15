"""Device state rules without database, HTTP or application dependencies."""


def key_error(device, now):
    if device is None or device['status'] != 'active':
        return 'ai_access_invalid_key'
    if min(device['expires_at'], device['hard_expires_at']) <= now:
        return 'ai_access_confirmation_required'
    if not device['acked'] and device.get('delivery_until', 0) <= now:
        return 'ai_access_activation_required'
    return None


def expiration_reason(device, enrollment, now):
    if device['status'] == 'revoked':
        return None
    abandoned = not device['acked'] and (
        not enrollment
        or enrollment.get('delivery_until', enrollment.get('activation_until', enrollment['expires_at'])) <= now
    )
    if abandoned or min(device['expires_at'], device['hard_expires_at']) <= now:
        return 'confirmation_expired' if device['hard_expires_at'] <= now else 'lease_or_delivery_expired'
    return None


def revoke(device, reason, now):
    """Update the caller's record; persistence and audit remain one transaction."""
    device.update(status='revoked', reason=reason, revoked_at=now, block_synced=False)
