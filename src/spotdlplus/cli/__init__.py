'''
cli. The terminal frontend, and nothing but.

The only layer allowed to import rich and typer, the only layer allowed to
print, and the layer with the least logic in it. Everything it renders arrived
as an event. Everything it triggers is one pipeline call.
'''
