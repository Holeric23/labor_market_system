"""Compatibility entry point: operational review moved to the authenticated web app."""


def create_server(port=8765):
    raise RuntimeError('Legacy editor is read-only. Use the OWNER review pages in market_system.web.')


if __name__=='__main__':
    create_server()
