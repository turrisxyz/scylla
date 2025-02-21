# -*- coding: utf-8 -*-
# Copyright 2021-present ScyllaDB
#
# SPDX-License-Identifier: AGPL-3.0-or-later

#############################################################################
# Tests for user defined functions defined with WebAssembly backend
#############################################################################

from cassandra.protocol import InvalidRequest
from cassandra.cluster import NoHostAvailable
from util import new_test_table, unique_name, new_function

import pytest
import os.path

# Can be used for marking functions which require
# WASM support to be compiled into Scylla
@pytest.fixture(scope="module")
def scylla_with_wasm_only(scylla_only, cql, test_keyspace):
    try:
        f42 = unique_name()
        f42_body = f'(module(func ${f42} (param $n i64) (result i64)(return i64.const 42))(export "{f42}" (func ${f42})))'
        res = cql.execute(f"CREATE FUNCTION {test_keyspace}.{f42} (input int) RETURNS NULL ON NULL INPUT RETURNS int LANGUAGE xwasm AS '{f42_body}'")
        cql.execute(f"DROP FUNCTION {test_keyspace}.{f42}")
    except NoHostAvailable as err:
        if "not enabled" in str(err):
            pytest.skip("WASM support was not enabled in Scylla, skipping")
    yield

@pytest.fixture(scope="module")
def table1(cql, test_keyspace):
    table = test_keyspace + "." + unique_name()
    cql.execute("CREATE TABLE " + table +
        "(p bigint PRIMARY KEY, p2 bigint, i int, i2 int, s smallint, s2 smallint, t tinyint, t2 tinyint, d double, f float, bl boolean, txt text)")
    yield table
    cql.execute("DROP TABLE " + table)

# Test that calling a wasm-based fibonacci function works
def test_fib(cql, test_keyspace, table1, scylla_with_wasm_only):
    table = table1
    fib_name = unique_name()
    fib_source = f"""
(module
  (func ${fib_name} (param $n i64) (result i64)
    (if
      (i64.lt_s (local.get $n) (i64.const 2))
      (return (local.get $n))
    )
    (i64.add
      (call ${fib_name} (i64.sub (local.get $n) (i64.const 1)))
      (call ${fib_name} (i64.sub (local.get $n) (i64.const 2)))
    )
  )
  (export "{fib_name}" (func ${fib_name}))
)
"""
    src = f"(input bigint) RETURNS NULL ON NULL INPUT RETURNS bigint LANGUAGE xwasm AS '{fib_source}'"
    with new_function(cql, test_keyspace, src, fib_name):
        cql.execute(f"INSERT INTO {table1} (p) VALUES (10)")
        res = [row for row in cql.execute(f"SELECT {test_keyspace}.{fib_name}(p) AS result FROM {table} WHERE p = 10")]
        assert len(res) == 1 and res[0].result == 55

        cql.execute(f"INSERT INTO {table} (p) VALUES (14)")
        res = [row for row in cql.execute(f"SELECT {test_keyspace}.{fib_name}(p) AS result FROM {table} WHERE p = 14")]
        assert len(res) == 1 and res[0].result == 377

        # This function returns null on null values
        res = [row for row in cql.execute(f"SELECT {test_keyspace}.{fib_name}(p2) AS result FROM {table} WHERE p = 14")]
        assert len(res) == 1 and res[0].result is None

        cql.execute(f"INSERT INTO {table} (p) VALUES (997)")
        # The call request takes too much time and resources, and should therefore fail
        with pytest.raises(InvalidRequest, match="wasm"):
            cql.execute(f"SELECT {test_keyspace}.{fib_name}(p) AS result FROM {table} WHERE p = 997")

# Test that calling a fibonacci function that claims to accept null input works.
# Note that since the int field is nullable, it's no longer
# passed as a simple param, but instead as a pointer to a structure with a serialized
# integer underneath - which follows the C ABI for WebAssembly.
# Also, note that CQL serializes integers as big endian, which means that
# WebAssembly should convert to host endianness (assumed little endian here)
# before operating on its native types.
# Compiled from:
# const int WASM_PAGE_SIZE = 64 * 1024;
# const int _scylla_abi = 0;
#
# static long long swap_int64(long long val) {
#     val = ((val << 8) & 0xFF00FF00FF00FF00ULL ) | ((val >> 8) & 0x00FF00FF00FF00FFULL );
#     val = ((val << 16) & 0xFFFF0000FFFF0000ULL ) | ((val >> 16) & 0x0000FFFF0000FFFFULL );
#     return (val << 32) | ((val >> 32) & 0xFFFFFFFFULL);
# }
#
# long long fib_aux(long long n) {
#     if (n < 2) {
#         return n;
#     }
#     return fib_aux(n-1) + fib_aux(n-2);
# }
# long long fib(long long p) {
#     int size = p >> 32;
#     long long* p_val = (long long*)(p & 0xffffffff);
#     // Initialize memory for the return value
#     long long* ret_val = (long long*)(__builtin_wasm_memory_size(0) * WASM_PAGE_SIZE);
#     __builtin_wasm_memory_grow(0, 1); // long long fits in one wasm page
#     if (size == -1) {
#         *ret_val = swap_int64(42);
#     } else {
#         *ret_val = swap_int64(fib_aux(swap_int64(*p_val)));
#     }
#     // 8 is the size of a bigint
#     return (long long)(8ll << 32) | (long long)ret_val;
# }
#
# with:
# $ clang -O2  --target=wasm32 --no-standard-libraries -Wl,--export=fib -Wl,--export=_scylla_abi -Wl,--no-entry fibnull.c -o fibnull.wasm
# $ wasm2wat fibnull.wasm > fibnull.wat
def test_fib_called_on_null(cql, test_keyspace, table1, scylla_with_wasm_only):
    table = table1
    fib_name = unique_name()
    fib_source = f"""
(module
  (type (;0;) (func (param i64) (result i64)))
  (func (;0;) (type 0) (param i64) (result i64)
    (local i64 i32)
    local.get 0
    i64.const 2
    i64.lt_s
    if  ;; label = @1
      local.get 0
      return
    end
    loop  ;; label = @1
      local.get 0
      i64.const 1
      i64.sub
      call 0
      local.get 1
      i64.add
      local.set 1
      local.get 0
      i64.const 3
      i64.gt_s
      local.set 2
      local.get 0
      i64.const 2
      i64.sub
      local.set 0
      local.get 2
      br_if 0 (;@1;)
    end
    local.get 0
    local.get 1
    i64.add)
  (func (;1;) (type 0) (param i64) (result i64)
    (local i32 i64)
    memory.size
    local.set 1
    i32.const 1
    memory.grow
    drop
    i64.const 3026418949592973312
    local.set 2
    local.get 1
    i32.const 16
    i32.shl
    local.tee 1
    local.get 0
    i64.const -4294967297
    i64.le_u
    if (result i64)  ;; label = @1
      local.get 0
      i32.wrap_i64
      i64.load
      local.tee 0
      i64.const 56
      i64.shl
      local.get 0
      i64.const 40
      i64.shl
      i64.const 71776119061217280
      i64.and
      i64.or
      local.get 0
      i64.const 24
      i64.shl
      i64.const 280375465082880
      i64.and
      local.get 0
      i64.const 8
      i64.shl
      i64.const 1095216660480
      i64.and
      i64.or
      i64.or
      local.get 0
      i64.const 8
      i64.shr_u
      i64.const 4278190080
      i64.and
      local.get 0
      i64.const 24
      i64.shr_u
      i64.const 16711680
      i64.and
      i64.or
      local.get 0
      i64.const 40
      i64.shr_u
      i64.const 65280
      i64.and
      local.get 0
      i64.const 56
      i64.shr_u
      i64.or
      i64.or
      i64.or
      call 0
      local.tee 0
      i64.const 56
      i64.shl
      local.get 0
      i64.const 40
      i64.shl
      i64.const 71776119061217280
      i64.and
      i64.or
      local.get 0
      i64.const 24
      i64.shl
      i64.const 280375465082880
      i64.and
      local.get 0
      i64.const 8
      i64.shl
      i64.const 1095216660480
      i64.and
      i64.or
      i64.or
      local.get 0
      i64.const 8
      i64.shr_u
      i64.const 4278190080
      i64.and
      local.get 0
      i64.const 24
      i64.shr_u
      i64.const 16711680
      i64.and
      i64.or
      local.get 0
      i64.const 40
      i64.shr_u
      i64.const 65280
      i64.and
      local.get 0
      i64.const 56
      i64.shr_u
      i64.or
      i64.or
      i64.or
    else
      local.get 2
    end
    i64.store
    local.get 1
    i64.extend_i32_u
    i64.const 34359738368
    i64.or)
  (memory (;0;) 2)
  (global (;0;) i32 (i32.const 1024))
  (export "memory" (memory 0))
  (export "{fib_name}" (func 1))
  (export "_scylla_abi" (global 0))
  (data (;0;) (i32.const 1024) "\\01"))
"""
    src = f"(input bigint) CALLED ON NULL INPUT RETURNS bigint LANGUAGE xwasm AS '{fib_source}'"
    with new_function(cql, test_keyspace, src, fib_name):
        cql.execute(f"INSERT INTO {table1} (p) VALUES (3)")
        res = [row for row in cql.execute(f"SELECT {test_keyspace}.{fib_name}(p) AS result FROM {table} WHERE p = 3")]
        assert len(res) == 1 and res[0].result == 2

        cql.execute(f"INSERT INTO {table} (p) VALUES (7)")
        res = [row for row in cql.execute(f"SELECT {test_keyspace}.{fib_name}(p) AS result FROM {table} WHERE p = 7")]
        assert len(res) == 1 and res[0].result == 13

        # Special semantics defined for null input in our function is to return "42"
        res = [row for row in cql.execute(f"SELECT {test_keyspace}.{fib_name}(p2) AS result FROM {table} WHERE p = 7")]
        assert len(res) == 1 and res[0].result == 42

        cql.execute(f"INSERT INTO {table} (p) VALUES (997)")
        # The call request takes too much time and resources, and should therefore fail
        with pytest.raises(InvalidRequest, match="wasm"):
          cql.execute(f"SELECT {test_keyspace}.{fib_name}(p) AS result FROM {table} WHERE p = 997")

# Test that an infinite loop gets broken out of eventually
def test_infinite_loop(cql, test_keyspace, table1, scylla_with_wasm_only):
    table = table1
    inf_loop_name = "inf_loop_" + unique_name()
    inf_loop_source = f"""
(module
  (type (;0;) (func (param i32) (result i32)))
  (func ${inf_loop_name} (type 0) (param i32) (result i32)
    loop (result i32)  ;; label = @1
      br 0 (;@1;)
    end)
  (table (;0;) 1 1 funcref)
  (table (;1;) 32 externref)
  (memory (;0;) 17)
  (export "{inf_loop_name}" (func ${inf_loop_name}))
  (elem (;0;) (i32.const 0) func)
  (global (;0;) i32 (i32.const 1024))
  (export "_scylla_abi" (global 0))
  (data $.rodata (i32.const 1024) "\\01"))
"""
    src = f"(input int) RETURNS NULL ON NULL INPUT RETURNS int LANGUAGE xwasm AS '{inf_loop_source}'"
    with new_function(cql, test_keyspace, src, inf_loop_name):
        cql.execute(f"INSERT INTO {table} (p,i) VALUES (10, 10)")
        import time
        start = time.monotonic()
        with pytest.raises(InvalidRequest, match="fuel consumed"):
            cql.execute(f"SELECT {test_keyspace}.{inf_loop_name}(i) AS result FROM {table} WHERE p = 10")
        elapsed_s = time.monotonic() - start
        print(f"Breaking the loop took {elapsed_s*1000:.2f}ms")

# Test a wasm function which decreases given double by 1
def test_f64_param(cql, test_keyspace, table1, scylla_with_wasm_only):
    table = table1
    dec_double_name = "dec_double_" + unique_name()
    dec_double_source = f"""
(module
  (type (;0;) (func (param f64) (result f64)))
  (func ${dec_double_name} (type 0) (param f64) (result f64)
    local.get 0
    f64.const -0x1p+0 (;=-1;)
    f64.add)
  (table (;0;) 1 1 funcref)
  (table (;1;) 32 externref)
  (memory (;0;) 17)
  (export "{dec_double_name}" (func ${dec_double_name}))
  (elem (;0;) (i32.const 0) func)
  (global (;0;) i32 (i32.const 1024))
  (export "_scylla_abi" (global 0))
  (data $.rodata (i32.const 1024) "\\01"))
"""
    src = f"(input double) RETURNS NULL ON NULL INPUT RETURNS double LANGUAGE xwasm AS '{dec_double_source}'"
    with new_function(cql, test_keyspace, src, dec_double_name):
        cql.execute(f"INSERT INTO {table} (p,d) VALUES (17,17.015625)")
        res = [row for row in cql.execute(f"SELECT {test_keyspace}.{dec_double_name}(d) AS result FROM {table} WHERE p = 17")]
        assert len(res) == 1 and res[0].result == 16.015625

# Test a wasm function which increases given float by 1
def test_f32_param(cql, test_keyspace, table1, scylla_with_wasm_only):
    table = table1
    inc_float_name = "inc_float_" + unique_name()
    inc_float_source = f"""
(module
  (type (;0;) (func (param f32) (result f32)))
  (func ${inc_float_name} (type 0) (param f32) (result f32)
    local.get 0
    f32.const 0x1p+0 (;=1;)
    f32.add)
  (table (;0;) 1 1 funcref)
  (table (;1;) 32 externref)
  (memory (;0;) 17)
  (export "{inc_float_name}" (func ${inc_float_name}))
  (elem (;0;) (i32.const 0) func)
  (global (;0;) i32 (i32.const 1024))
  (export "_scylla_abi" (global 0))
  (data $.rodata (i32.const 1024) "\\01"))
"""
    src = f"(input float) RETURNS NULL ON NULL INPUT RETURNS float LANGUAGE xwasm AS '{inc_float_source}'"
    with new_function(cql, test_keyspace, src, inc_float_name):
        cql.execute(f"INSERT INTO {table} (p, f) VALUES (121, 121.00390625)")
        res = [row for row in cql.execute(f"SELECT {test_keyspace}.{inc_float_name}(f) AS result FROM {table} WHERE p = 121")]
        assert len(res) == 1 and res[0].result == 122.00390625

# Test a wasm function which operates on booleans
def test_bool_negate(cql, test_keyspace, table1, scylla_with_wasm_only):
    table = table1
    negate_name = "negate_" + unique_name()
    negate_source = f"""
(module
  (type (;0;) (func (param i32) (result i32)))
  (func ${negate_name} (type 0) (param i32) (result i32)
    local.get 0
    i32.eqz)
  (table (;0;) 1 1 funcref)
  (table (;1;) 32 externref)
  (memory (;0;) 17)
  (export "{negate_name}" (func ${negate_name}))
  (elem (;0;) (i32.const 0) func)
  (global (;0;) i32 (i32.const 1024))
  (export "_scylla_abi" (global 0))
  (data $.rodata (i32.const 1024) "\\01"))
"""
    src = f"(input boolean) RETURNS NULL ON NULL INPUT RETURNS boolean LANGUAGE xwasm AS '{negate_source}'"
    with new_function(cql, test_keyspace, src, negate_name):
        cql.execute(f"INSERT INTO {table} (p, bl) VALUES (19, true)")
        cql.execute(f"INSERT INTO {table} (p, bl) VALUES (21, false)")
        res = [row for row in cql.execute(f"SELECT {test_keyspace}.{negate_name}(bl) AS result FROM {table} WHERE p = 19")]
        assert len(res) == 1 and res[0].result == False
        res = [row for row in cql.execute(f"SELECT {test_keyspace}.{negate_name}(bl) AS result FROM {table} WHERE p = 21")]
        assert len(res) == 1 and res[0].result == True

# Test wasm functions which operate on 8bit and 16bit integers,
# which are simulated by 32bit integers by wasm anyway
def test_short_ints(cql, test_keyspace, table1, scylla_with_wasm_only):
    table = table1
    plus_name = "plus_" + unique_name()
    plus_source = f"""
(module
  (type (;0;) (func (param i32 i32) (result i32)))
  (func ${plus_name} (type 0) (param i32 i32) (result i32)
    local.get 1
    local.get 0
    i32.add)
  (table (;0;) 1 1 funcref)
  (table (;1;) 32 externref)
  (memory (;0;) 17)
  (export "{plus_name}" (func ${plus_name}))
  (elem (;0;) (i32.const 0) func)
  (global (;0;) i32 (i32.const 1024))
  (export "_scylla_abi" (global 0))
  (data $.rodata (i32.const 1024) "\\01"))
"""
    src = f"(input tinyint, input2 tinyint) RETURNS NULL ON NULL INPUT RETURNS tinyint LANGUAGE xwasm AS '{plus_source}'"
    with new_function(cql, test_keyspace, src, plus_name):
        cql.execute(f"INSERT INTO {table} (p, t, t2, s, s2) VALUES (42, 42, 24, 33, 55)")
        cql.execute(f"INSERT INTO {table} (p, t, t2, s, s2) VALUES (43, 120, 112, 32000, 24001)")
        res = [row for row in cql.execute(f"SELECT {test_keyspace}.{plus_name}(t, t2) AS result FROM {table} WHERE p = 42")]
        assert len(res) == 1 and res[0].result == 66
        # Overflow is fine
        res = [row for row in cql.execute(f"SELECT {test_keyspace}.{plus_name}(t, t2) AS result FROM {table} WHERE p = 43")]
        assert len(res) == 1 and res[0].result == -24
    # A similar run for 16bit ints - note that the exact same source code is used
    src = f"(input smallint, input2 smallint) RETURNS NULL ON NULL INPUT RETURNS smallint LANGUAGE xwasm AS '{plus_source}'"
    with new_function(cql, test_keyspace, src, plus_name):
        res = [row for row in cql.execute(f"SELECT {test_keyspace}.{plus_name}(s, s2) AS result FROM {table} WHERE p = 42")]
        assert len(res) == 1 and res[0].result == 88
        # Overflow is fine
        res = [row for row in cql.execute(f"SELECT {test_keyspace}.{plus_name}(s, s2) AS result FROM {table} WHERE p = 43")]
        assert len(res) == 1 and res[0].result == -9535

# Test that passing a large number of params works fine
def test_9_params(cql, test_keyspace, table1, scylla_with_wasm_only):
    table = table1
    sum9_name = "sum9_" + unique_name()
    sum9_source = f"""
(module
  (type (;0;) (func (param i32 i32 i32 i32 i32 i32 i32 i32 i32) (result i32)))
  (func ${sum9_name} (type 0) (param i32 i32 i32 i32 i32 i32 i32 i32 i32) (result i32)
    local.get 1
    local.get 0
    i32.add
    local.get 2
    i32.add
    local.get 3
    i32.add
    local.get 4
    i32.add
    local.get 5
    i32.add
    local.get 6
    i32.add
    local.get 7
    i32.add
    local.get 8
    i32.add)
  (table (;0;) 1 1 funcref)
  (table (;1;) 32 externref)
  (memory (;0;) 17)
  (export "{sum9_name}" (func ${sum9_name}))
  (elem (;0;) (i32.const 0) func)
  (global (;0;) i32 (i32.const 1024))
  (export "_scylla_abi" (global 0))
  (data $.rodata (i32.const 1024) "\\01"))

"""
    src = f"(a int, b int, c int, d int, e int, f int, g int, h int, i int) RETURNS NULL ON NULL INPUT RETURNS int LANGUAGE xwasm AS '{sum9_source}'"
    with new_function(cql, test_keyspace, src, sum9_name):
        cql.execute(f"INSERT INTO {table} (p, i, i2) VALUES (777, 1,2)")
        res = [row for row in cql.execute(f"SELECT {test_keyspace}.{sum9_name}(i,i2,i2,i,i2,i,i2,i,i2) AS result FROM {table} WHERE p = 777")]
        assert len(res) == 1 and res[0].result == 14

# Test a wasm function which takes 2 arguments - a base and a power - and returns base**power
def test_pow(cql, test_keyspace, table1, scylla_with_wasm_only):
    table = table1
    pow_name = "pow_" + unique_name()
    pow_source = f"""
(module
  (type (;0;) (func (param i32 i32) (result i32)))
  (func ${pow_name} (type 0) (param i32 i32) (result i32)
    (local i32 i32)
    i32.const 1
    local.set 2
    block  ;; label = @1
      block  ;; label = @2
        block  ;; label = @3
          local.get 1
          br_table 2 (;@1;) 1 (;@2;) 0 (;@3;)
        end
        local.get 1
        local.set 2
        i32.const 1
        local.set 1
        loop  ;; label = @3
          local.get 0
          i32.const 1
          local.get 2
          i32.const 1
          i32.and
          select
          local.get 1
          i32.mul
          local.set 1
          local.get 2
          i32.const 3
          i32.gt_u
          local.set 3
          local.get 0
          local.get 0
          i32.mul
          local.set 0
          local.get 2
          i32.const 1
          i32.shr_u
          local.set 2
          local.get 3
          br_if 0 (;@3;)
        end
      end
      local.get 0
      local.get 1
      i32.mul
      local.set 2
    end
    local.get 2)
  (table (;0;) 1 1 funcref)
  (table (;1;) 32 externref)
  (memory (;0;) 17)
  (global (;0;) i32 (i32.const 1024))
  (export "{pow_name}" (func ${pow_name}))
  (elem (;0;) (i32.const 0) func)
  (export "_scylla_abi" (global 0))
  (data $.rodata (i32.const 1024) "\\01"))
"""
    src = f"(base int, pow int) RETURNS NULL ON NULL INPUT RETURNS int LANGUAGE xwasm AS '{pow_source}'"
    with new_function(cql, test_keyspace, src, pow_name):
        cql.execute(f"INSERT INTO {table} (p, i, i2) VALUES (311, 3, 11)")
        res = [row for row in cql.execute(f"SELECT {test_keyspace}.{pow_name}(i, i2) AS result FROM {table} WHERE p = 311")]
        assert len(res) == 1 and res[0].result == 177147

# Test that only compilable input is accepted
def test_compilable(cql, test_keyspace, table1, scylla_with_wasm_only):
    table = table1
    wrong_source = f"""
Dear wasmtime compiler, please return a function which returns its float argument increased by 1
"""
    with pytest.raises(InvalidRequest, match="Compilation failed"):
      cql.execute(f"CREATE FUNCTION {test_keyspace}.i_was_not_exported (input float) RETURNS NULL ON NULL INPUT RETURNS float LANGUAGE xwasm "
                f"AS '{wrong_source}'")

# Test that not exporting a function with matching name
# results in an error
def test_not_exported(cql, test_keyspace, table1, scylla_with_wasm_only):
    table = table1
    wrong_source = f"""
(module
  (type (;0;) (func (param f32) (result f32)))
  (func $i_was_not_exported (type 0) (param f32) (result f32)
    local.get 0
    f32.const 0x1p+0 (;=1;)
    f32.add)
  (table (;0;) 1 1 funcref)
  (table (;1;) 32 externref)
  (memory (;0;) 17)
  (elem (;0;) (i32.const 0) func))
"""
    with pytest.raises(InvalidRequest, match="not found"):
        cql.execute(f"CREATE FUNCTION {test_keyspace}.i_was_not_exported (input float) RETURNS NULL ON NULL INPUT RETURNS float LANGUAGE xwasm "
                f"AS '{wrong_source}'")

# Test that trying to use something that is exported, but is not a function, won't work
def test_not_a_function(cql, test_keyspace, table1, scylla_with_wasm_only):
    table = table1
    wrong_source = f"""
(module
  (type (;0;) (func (param f32) (result f32)))
  (func $i_was_not_exported (type 0) (param f32) (result f32)
    local.get 0
    f32.const 0x1p+0 (;=1;)
    f32.add)
  (table (;0;) 1 1 funcref)
  (table (;1;) 32 externref)
  (memory (;0;) 17)
  (global (;0;) (mut i32) (i32.const 1048576))
  (global (;1;) i32 (i32.const 1024))
  (export "memory" (memory 0))
  (elem (;0;) (i32.const 0) func)
  (export "_scylla_abi" (global 1))
  (data $.rodata (i32.const 1024) "\\01"))
"""
    with pytest.raises(InvalidRequest, match="not a function"):
        cql.execute(f"CREATE FUNCTION {test_keyspace}.memory (input float) RETURNS NULL ON NULL INPUT RETURNS float LANGUAGE xwasm "
                f"AS '{wrong_source}'")

# Test that the function should accept only the correct number and types of params
def test_validate_params(cql, test_keyspace, table1, scylla_with_wasm_only):
    table = table1
    inc_float_name = "inc_float_" + unique_name()
    inc_float_source = f"""
(module
  (type (;0;) (func (param f32) (result f32)))
  (func ${inc_float_name} (type 0) (param f32) (result f32)
    local.get 0
    f32.const 0x1p+0 (;=1;)
    f32.add)
  (table (;0;) 1 1 funcref)
  (table (;1;) 32 externref)
  (memory (;0;) 17)
  (global (;0;) i32 (i32.const 1024))
  (export "memory" (memory 0))
  (export "{inc_float_name}" (func ${inc_float_name}))
  (elem (;0;) (i32.const 0) func)
  (export "_scylla_abi" (global 0))
  (data $.rodata (i32.const 1024) "\\01"))
"""
    src = f"(input int) RETURNS NULL ON NULL INPUT RETURNS float LANGUAGE xwasm AS '{inc_float_source}'"
    with new_function(cql, test_keyspace, src, inc_float_name):
        cql.execute(f"INSERT INTO {table} (p, i, f, txt) VALUES (700, 7, 7., 'oi')")
        with pytest.raises(InvalidRequest, match="type mismatch"):
            cql.execute(f"SELECT {test_keyspace}.{inc_float_name}(i) AS result FROM {table} WHERE p = 700")
    src = f"(input text) RETURNS NULL ON NULL INPUT RETURNS int LANGUAGE xwasm AS '{inc_float_source}'"
    with new_function(cql, test_keyspace, src, inc_float_name):
        with pytest.raises(InvalidRequest, match="failed"):
            cql.execute(f"SELECT {test_keyspace}.{inc_float_name}(txt) AS result FROM {table} WHERE p = 700")
    src = f"(input float) RETURNS NULL ON NULL INPUT RETURNS int LANGUAGE xwasm AS '{inc_float_source}'"
    with new_function(cql, test_keyspace, src, inc_float_name):
        with pytest.raises(InvalidRequest, match="Expected i32, got f32"):
            cql.execute(f"SELECT {test_keyspace}.{inc_float_name}(f) AS result FROM {table} WHERE p = 700")
        with pytest.raises(InvalidRequest, match="number.*arguments"):
            cql.execute(f"SELECT {test_keyspace}.{inc_float_name}(i, f) AS result FROM {table} WHERE p = 700")

# Test that calling a wasm-based function on a string works.
# The function doubles the string: dog -> dogdog.
# Created with:
# const int WASM_PAGE_SIZE = 64 * 1024;
# const int _scylla_abi = 0;

# long long dbl(long long par) {
#    int size = par >> 32;
#    int position = par & 0xffffffff;
#    int orig_size = __builtin_wasm_memory_size(0) * WASM_PAGE_SIZE;
#    __builtin_wasm_memory_grow(0, 1 + (2 * size - 1) / WASM_PAGE_SIZE);
#    char* p = (char*)0;
#    for (int i = 0; i < size; ++i) {
#        p[orig_size + i] = p[position + i];
#        p[orig_size + size + i] = p[position + i];
#    }
#    long long ret = ((long long)2 * size << 32) | (long long)orig_size;
#    return ret;
# }
# ... and compiled with
# clang --target=wasm32 --no-standard-libraries -Wl,--export=dbl -Wl,--export=_scylla_abi -Wl,--no-entry demo.c -o demo.wasm
# wasm2wat demo.wasm > demo.wat

def test_word_double(cql, test_keyspace, table1, scylla_with_wasm_only):
    table = table1
    dbl_name = unique_name()
    dbl_source = f"""
(module
  (type (;0;) (func (param i64) (result i64)))
  (func $dbl (type 0) (param i64) (result i64)
    (local i32 i32 i32 i64 i64 i64 i32 i64 i64 i64 i32 i32 i32 i32 i32 i32 i32 i32 i32 i32 i32 i32 i32 i32 i32 i32 i32 i32 i32 i32 i32 i32 i32 i32 i32 i32 i32 i32 i32 i32 i32 i32 i32 i32 i32 i32 i32 i32 i32 i32 i32 i32 i32 i32 i32 i32 i32 i32 i32 i32 i32 i64 i64 i64 i64 i64 i32 i32 i64 i64 i64)
    global.get $__stack_pointer
    local.set 1
    i32.const 48
    local.set 2
    local.get 1
    local.get 2
    i32.sub
    local.set 3
    local.get 3
    local.get 0
    i64.store offset=40
    local.get 3
    i64.load offset=40
    local.set 4
    i64.const 32
    local.set 5
    local.get 4
    local.get 5
    i64.shr_s
    local.set 6
    local.get 6
    i32.wrap_i64
    local.set 7
    local.get 3
    local.get 7
    i32.store offset=36
    local.get 3
    i64.load offset=40
    local.set 8
    i64.const 4294967295
    local.set 9
    local.get 8
    local.get 9
    i64.and
    local.set 10
    local.get 10
    i32.wrap_i64
    local.set 11
    local.get 3
    local.get 11
    i32.store offset=32
    memory.size
    local.set 12
    i32.const 16
    local.set 13
    local.get 12
    local.get 13
    i32.shl
    local.set 14
    local.get 3
    local.get 14
    i32.store offset=28
    local.get 3
    i32.load offset=36
    local.set 15
    i32.const 1
    local.set 16
    local.get 15
    local.get 16
    i32.shl
    local.set 17
    i32.const 1
    local.set 18
    local.get 17
    local.get 18
    i32.sub
    local.set 19
    i32.const 65536
    local.set 20
    local.get 19
    local.get 20
    i32.div_s
    local.set 21
    i32.const 1
    local.set 22
    local.get 21
    local.get 22
    i32.add
    local.set 23
    local.get 23
    memory.grow
    drop
    i32.const 0
    local.set 24
    local.get 3
    local.get 24
    i32.store offset=24
    i32.const 0
    local.set 25
    local.get 3
    local.get 25
    i32.store offset=20
    block  ;; label = @1
      loop  ;; label = @2
        local.get 3
        i32.load offset=20
        local.set 26
        local.get 3
        i32.load offset=36
        local.set 27
        local.get 26
        local.set 28
        local.get 27
        local.set 29
        local.get 28
        local.get 29
        i32.lt_s
        local.set 30
        i32.const 1
        local.set 31
        local.get 30
        local.get 31
        i32.and
        local.set 32
        local.get 32
        i32.eqz
        br_if 1 (;@1;)
        local.get 3
        i32.load offset=24
        local.set 33
        local.get 3
        i32.load offset=32
        local.set 34
        local.get 3
        i32.load offset=20
        local.set 35
        local.get 34
        local.get 35
        i32.add
        local.set 36
        local.get 33
        local.get 36
        i32.add
        local.set 37
        local.get 37
        i32.load8_u
        local.set 38
        local.get 3
        i32.load offset=24
        local.set 39
        local.get 3
        i32.load offset=28
        local.set 40
        local.get 3
        i32.load offset=20
        local.set 41
        local.get 40
        local.get 41
        i32.add
        local.set 42
        local.get 39
        local.get 42
        i32.add
        local.set 43
        local.get 43
        local.get 38
        i32.store8
        local.get 3
        i32.load offset=24
        local.set 44
        local.get 3
        i32.load offset=32
        local.set 45
        local.get 3
        i32.load offset=20
        local.set 46
        local.get 45
        local.get 46
        i32.add
        local.set 47
        local.get 44
        local.get 47
        i32.add
        local.set 48
        local.get 48
        i32.load8_u
        local.set 49
        local.get 3
        i32.load offset=24
        local.set 50
        local.get 3
        i32.load offset=28
        local.set 51
        local.get 3
        i32.load offset=36
        local.set 52
        local.get 51
        local.get 52
        i32.add
        local.set 53
        local.get 3
        i32.load offset=20
        local.set 54
        local.get 53
        local.get 54
        i32.add
        local.set 55
        local.get 50
        local.get 55
        i32.add
        local.set 56
        local.get 56
        local.get 49
        i32.store8
        local.get 3
        i32.load offset=20
        local.set 57
        i32.const 1
        local.set 58
        local.get 57
        local.get 58
        i32.add
        local.set 59
        local.get 3
        local.get 59
        i32.store offset=20
        br 0 (;@2;)
      end
    end
    local.get 3
    i32.load offset=36
    local.set 60
    local.get 60
    local.set 61
    local.get 61
    i64.extend_i32_s
    local.set 62
    i64.const 1
    local.set 63
    local.get 62
    local.get 63
    i64.shl
    local.set 64
    i64.const 32
    local.set 65
    local.get 64
    local.get 65
    i64.shl
    local.set 66
    local.get 3
    i32.load offset=28
    local.set 67
    local.get 67
    local.set 68
    local.get 68
    i64.extend_i32_s
    local.set 69
    local.get 66
    local.get 69
    i64.or
    local.set 70
    local.get 3
    local.get 70
    i64.store offset=8
    local.get 3
    i64.load offset=8
    local.set 71
    local.get 71
    return)
  (memory (;0;) 2)
  (global $__stack_pointer (mut i32) (i32.const 66576))
  (global (;1;) i32 (i32.const 1024))
  (export "memory" (memory 0))
  (export "{dbl_name}" (func $dbl))
  (export "_scylla_abi" (global 1))
  (data $.rodata (i32.const 1024) "\\01"))
"""
    src = f"(input text) RETURNS NULL ON NULL INPUT RETURNS text LANGUAGE xwasm AS '{dbl_source}'"
    with new_function(cql, test_keyspace, src, dbl_name):
        cql.execute(f"INSERT INTO {table1} (p, txt) VALUES (1000, 'doggo')")
        res = [row for row in cql.execute(f"SELECT {test_keyspace}.{dbl_name}(txt) AS result FROM {table} WHERE p = 1000")]
        assert len(res) == 1 and res[0].result == 'doggodoggo'

        cql.execute(f"INSERT INTO {table} (p, txt) VALUES (1001, 'cat42')")
        res = [row for row in cql.execute(f"SELECT {test_keyspace}.{dbl_name}(txt) AS result FROM {table} WHERE p = 1001")]
        assert len(res) == 1 and res[0].result == 'cat42cat42'

# Test that calling a wasm-based function works with ABI version 2.
# The function returns the input. It's compatible with all data types represented by size + pointer.
# Created with:
# # extern "C" {
#     fn malloc(size: usize) -> usize;
#     fn free(ptr: *mut usize);
# }

# #[no_mangle]
# pub unsafe extern "C" fn _scylla_malloc(size: usize) -> u32 {
#     malloc(size) as u32
# }

# #[no_mangle]
# pub unsafe extern "C" fn _scylla_free(ptr: *mut usize) {
#     free(ptr)
# }

# #[no_mangle]
# pub static _scylla_abi: u32 = 2;

# #[no_mangle]
# pub extern "C" fn return_input(sizeptr: u64) -> u64 {
#     sizeptr
# }

# ... and compiled with
# cargo build --target=wasm32-wasi --release
# wasm2wat return_input.wasm > return_input.wat

def test_abi_v2(cql, test_keyspace, table1, scylla_with_wasm_only):
    table = table1
    ri_name = unique_name()
    wat_path = os.path.realpath(os.path.join(__file__, '../../resource/wasm/return_input.wat'))
    ri_source = open(wat_path, 'r').read().replace('export "return_input"', f'export "{ri_name}"')
    text_src = f"(input text) RETURNS NULL ON NULL INPUT RETURNS text LANGUAGE xwasm AS '{ri_source}'"
    with new_function(cql, test_keyspace, text_src, ri_name):
        cql.execute(f"INSERT INTO {table1} (p, txt) VALUES (2000, 'doggo')")
        res = [row for row in cql.execute(f"SELECT {test_keyspace}.{ri_name}(txt) AS result FROM {table} WHERE p = 2000")]
        assert len(res) == 1 and res[0].result == 'doggo'
