from slixmpp import Message


def xep_0045_rooms(xmpp, jid):
    """
    The MUC plugin has "multi" and "single" mode
    and you have to check which you're in in order
    to know where to look for what your MUC state is.

    This helper does that.

      Why?? Couldn't "multi" mode on be on permanently?

      - xmpp is the xmpp client/component
      - jid is the current user
    """
    if xmpp["xep_0045"].multi_from:
        return xmpp["xep_0045"].rooms[jid]
    else:
        return xmpp["xep_0045"].rooms[None]


def reply_id(msg: Message) -> str:
    reply_id = None
    # https://xmpp.org/extensions/xep-0461.html#business-id
    # # fuck u stanzaid
    # > in group chat situations, the ID assigned to the stanza by the group chat itself must be used.
    if msg["type"] == "groupchat":
        # >  This is discovered in a <stanza-id> element with a 'by' attribute that matches the bare JID of the group chat
        if msg["stanza_id"]["by"] == msg["from"].bare:
            reply_id = msg["stanza_id"]["id"]
    else:
        # reply_id = msg['origin_id']['id'] # usually the same as id?
        # >  For other message types [...] <origin-id> if present,
        # > or the value of the 'id' attribute on the <message> otherwise.
        if msg["origin_id"]["id"]:
            reply_id = msg["origin_id"]["id"]
        else:
            reply_id = msg["id"]

    return reply_id
