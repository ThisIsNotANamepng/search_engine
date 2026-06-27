# Code Styleguide

## Docstrings

Because this is a big project, there are a lot of functions. We need to add clear comments for each function describing how they work, and we need to stay consistent in how we do so.

We use a mix of Google style and numpy style docstrings for all of our functions

```
"""
A single-line description of what the function does

Args:
    price : float
        The price of a single item
    quantity : int
        The number of items being purchased

Returns:
    float
        The total cost (price times quantity)

Raises:
    ValueError : If quantity is negative

TODO:
    - Fix error when 0 divides 0
    - Make faster

Notes:
    Lol this is a funny function
    But also I've never tested it and we should test it
"""
```