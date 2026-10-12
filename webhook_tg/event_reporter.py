def report_who_update_event(
    *,
    chat_id,
    message_id,
    business_connection_id=None,
    username_from=None,
    first_name=None,
):
    """Compatibility hook: the saved Message is mirrored by the minute exporter.

    Do not start threads or send HTTP here. Message and operational metric rows
    are already durable, so the batch publisher can recover after a restart.
    """
    return None
