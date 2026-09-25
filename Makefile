# 瘦壳。**真正的逻辑全在 tasks.py**。
#
# Windows 默认没有 make，而这个项目的开发机就是 Windows —— 那边请用：
#     python tasks.py <命令>
#
# 这里保留 `make up` 这类肌肉记忆用法，以及给 Linux/macOS/CI 用。
# 新增任务时改 tasks.py，然后在这里加两行委托，不要在这里写逻辑。

PY ?= python
.DEFAULT_GOAL := help
.PHONY: help env lock sync build up down clean restart ps logs health \
        scale kill-worker test test-unit test-int test-e2e eval \
        lint fmt typecheck demo web-install web-dev

help:      ; @$(PY) tasks.py
env:       ; @$(PY) tasks.py env
lock:      ; @$(PY) tasks.py lock
sync:      ; @$(PY) tasks.py sync
build:     ; @$(PY) tasks.py build
up:        ; @$(PY) tasks.py up
down:      ; @$(PY) tasks.py down
clean:     ; @$(PY) tasks.py clean
restart:   ; @$(PY) tasks.py restart
ps:        ; @$(PY) tasks.py ps
logs:      ; @$(PY) tasks.py logs -f
health:    ; @$(PY) tasks.py health

scale:        ; @$(PY) tasks.py scale
kill-worker:  ; @$(PY) tasks.py kill-worker

test:      ; @$(PY) tasks.py test
test-unit: ; @$(PY) tasks.py test
test-int:  ; @$(PY) tasks.py test-int
test-e2e:  ; @$(PY) tasks.py test-e2e
eval:      ; @$(PY) tasks.py eval

lint:      ; @$(PY) tasks.py lint
fmt:       ; @$(PY) tasks.py fmt
typecheck: ; @$(PY) tasks.py typecheck

demo:         ; @$(PY) tasks.py demo
web-install:  ; @$(PY) tasks.py web-install
web-dev:      ; @$(PY) tasks.py web-dev
