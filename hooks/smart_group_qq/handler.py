"""Bridge Hermes gateway lifecycle to the loaded QQ plugin instance."""
import inspect


async def handle(event_type, context):
    if event_type != "gateway:startup":
        return
    from hermes_cli.plugins import get_plugin_subscriptions

    callbacks = get_plugin_subscriptions().get("smart_group_qq:gateway_startup", [])
    if not callbacks:
        raise RuntimeError("smart_group_qq startup subscription unavailable")
    for callback in callbacks:
        result = callback(context)
        if inspect.isawaitable(result):
            await result
