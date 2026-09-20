# typings-rubicon-objc

Python type hints for https://github.com/beeware/rubicon-objc.

## Usage

Copy this folder to your Python project's `typings/rubicon/objc` folder. This is just
the `objc` folder so you have to create the parent `rubicon` folder in order for
the type checking to work properly since the base package is `rubicon.objc`.

`typings` is a common folder name used by some static type checkers, but you can
use a different name for that folder. This is done so that it can also be used
as a git submodule or subtree as shown below.

```shell
git submodule add https://github.com/dlech/typings-rubicon-objc.git typings/rubicon/objc
```

```shell
git subtree add --prefix typings/rubicon/objc https://github.com/dlech/typings-rubicon-objc main
```
