"""Bridge Hermes gateway lifecycle to the loaded QQ plugin instance."""
import inspect


async def handle(event_type, context):
    if event_type != "gateway:startup":
        return
    from hermes_cli.plugins import get_plugin_manager

    manager = get_plugin_manager()
    event = "smart_group_qq:gateway_startup"
    # Hermes 0.22 stores owner-tagged subscriptions on the active profile manager.
    # Invoke on the gateway loop, rather than its synchronous event worker.
    with manager._event_lock:
        subscriptions = [
            item for item in manager._subscriptions.get(event, [])
            if item.owner == "smart_group_qq"
        ]
    if not subscriptions:
        raise RuntimeError("smart_group_qq startup subscription unavailable")
    for subscription in subscriptions:
        with manager._event_lock:
            if not any(item is subscription for item in manager._subscriptions.get(event, [])):
                continue
        result = subscription.callback(context)
        if inspect.isawaitable(result):
            await result
