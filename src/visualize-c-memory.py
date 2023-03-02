from shutil import which
from sys import stdout
import gdb      # pyright: reportMissingImports=false
import subprocess
import json
import html
import traceback

call_count_for_Svg = 0
### Register pretty printer ######################

class MemoryPrinter:
    def __init__(self):
        pass
    def to_string(self):
        return visualize_memory()

def lookup_printer(value):
    # Use MemoryPrinter if value is the string "memory"
    if value.type.strip_typedefs().code == gdb.TYPE_CODE_ARRAY and value.type.target().strip_typedefs().code == gdb.TYPE_CODE_INT and value.string() == "memory":
        return MemoryPrinter()
    else:
        return None

gdb.pretty_printers.append(lookup_printer)


### The actual visualization ########################

# Returns a json visualization of memory that can be consumed by vscode-debug-visualizer
def visualize_memory():
    try:
        return json.dumps({
            'kind': { 'svg': True },
            'text': svg_of_memory(),
        })
    except BaseException as e:
        # display errors using the text visualizer
        return json.dumps({
            'kind': { 'text': True },
            'text': str(e) + "\n\n\n\n\n\n\n" + traceback.format_exc()
        })

def svg_of_memory():
    memory = {
        'stack': recs_of_stack(),
        'heap': rec_of_heap(),
    }
    infer_heap_types(memory)

    # If the heap is too large, show only the last 100 entries
    if(len(memory['heap']['values']) > 100):
        memory['heap']['name'] = 'Heap (100 most recent entries)'
        memory['heap']['values'] = memory['heap']['values'][-100:]
        memory['heap']['fields'] = memory['heap']['fields'][-100:]

    dot = f"""
        digraph G {{
            layout = neato;
            overlap = false;
            node [style=dashed, shape=box];

            {dot_of_stack(memory)}
            dummy[pos="1,0!",style=invis,width=0.8];  // space
            {dot_of_heap(memory)}
            {dot_of_pointers(memory)}
        }}
    """

    # vscode-debug-visualizer can directly display graphviz dot format. However
    # its implementation has issues when the visualization is updated, after the
    # update it's often corrupted. Maybe this has to do with the fact that
    # vscode-debug-visualizer runs graphviz in wasm (via viz.js).
    #
    # To avoid the isses we run graphviz ourselves, convert to svg, and use the svg visualizer.
    # The downside is that graphviz needs to be installed.
    svg = subprocess.run(
        ['dot', '-T', 'svg'],
        input=dot.encode('utf-8'),
        stdout=subprocess.PIPE, stderr=subprocess.PIPE)

    if svg.returncode != 0:
        raise Exception(f"dot failed:\n {svg.stderr.decode('utf-8')}\n\ndot source:\n{dot}")
    ## 不想输出图片可以注释该段
    global call_count_for_Svg 
    call_count_for_Svg += 1
    filename = f"memory_{call_count_for_Svg}.svg"
    filepath = "../out/"+filename
    with open(filepath,'w') as f:
        f.write(svg.stdout.decode('utf-8'))
    ## 每次运行前在example文件夹内make clean_out一下情况输出文件夹
    return svg.stdout.decode('utf-8')

def dot_of_stack(memory):
    rows = [['<td>Stack</td>']]
    for frame_rec in memory['stack']:
        rows += rows_of_rec(frame_rec, memory)

    return f"""
        stack[pos="0,0!",label=<
            {table_of_rows(rows)}
        >];
    """

def dot_of_heap(memory):
    # pos="2,0" makes heap to be slightly on the right of stack/dummy.
    # overlap = false will force it further to the right, to avoid overlap.
    # but pos="2,0" is important to establish the relative position between the two.

    rows = rows_of_rec(memory['heap'], memory)
    return f"""
        heap[pos="2,0!",label=<
            {table_of_rows(rows)}
        >];
    """

def table_of_rows(rows):
    res = f"""
        <table border="0" cellborder="1" cellspacing="0" cellpadding="1">
    """

    col_n = max([len(row) for row in rows])
    for row in rows:
        # the last cell is the address, put it first
        row.insert(0, row.pop())

        # if the row has missing columns, add a colspan to the last cell
        if len(row) < col_n:
            row[-1] = row[-1].replace('<td', f'<td colspan="{col_n-len(row)+1}"')

        res += f'<tr>{"".join(row)}</tr>\n'

    res += '</table>'
    return res

def dot_of_pointers(memory):
    # construct   stack:"XXXXXXX-right":e  or  heap:"XXXXXX-left":w
    def anchor_of_rec(rec):
        return rec['area'] + ':"' + rec["address"] + ('-right":e' if rec['area'] == 'stack' else '-left":w')

    res = ""
    for rec in find_pointers(memory):
        target_rec = lookup_address(rec['value'], memory)
        if target_rec is not None:
            res += f"""
                {anchor_of_rec(rec)} -> {anchor_of_rec(target_rec)};
            """
    return res

def rows_of_rec(rec, memory):
    if rec['kind'] in ['array', 'struct', 'frame']:
        res = []
        for i in range(len(rec['values'])):
            name = rec['fields'][i] if rec['kind'] != 'array' else str(i)
            value_rec = rec['values'][i]
            rows = rows_of_rec(value_rec, memory)

            if len(rows) == 0:      # it can happen in case of empty array
                continue

            # the name is only inserted in the first row, with a rowspan to include all of them
            # an empty cell is also added to all other rows, so that len(row) always gives the number of cols
            rows[0].insert(0, f"""
                <td rowspan="{len(rows)}"><font point-size="11">{name}</font></td>
            """)
            for row in rows[1:]:
                row.insert(0, '')

            res += rows

        if rec['kind'] == 'frame':
            # at least 170 width, to avoid initial heap looking tiny
            res.insert(0, [f'<td width="170">{rec["name"]}</td>'])

    else:
        color = 'red' if rec['kind'] == 'pointer' and rec['value'] != "0" and lookup_address(rec['value'], memory) is None else 'black'
        res = [[
            f"""<td port="{rec['address']}-right"><font color="{color}" point-size="11">{rec['value']}</font></td>""",
            f"""<td port="{rec['address']}-left"><font point-size="9">{rec['address']} ({rec['size']})</font></td>""",
        ]]

    return res


def address_within_rec(address, rec):
    address_i = int(address, 16)
    rec_i = int(rec['address'], 16)
    return address_i >= rec_i and address_i < rec_i + rec['size']

# Check if address is within any of the known records, if so return that record
def lookup_address(address, memory):
    for rec in [memory['heap']] + memory['stack']:
        res = lookup_address_rec(address, rec)
        if res != None:
            return res
    return None

def lookup_address_rec(address, rec):
    if rec['kind'] in ['array', 'struct', 'frame']:
        for value in rec['values']:
            res = lookup_address_rec(address, value)
            if res != None:
                return res
        return None
    else:
        return rec if address_within_rec(address, rec) else None


# Check if any of the known (non-void) pointers points to address, if so return the rec of the pointer
def lookup_pointer(address, memory):
    for rec in find_pointers(memory):
        # exclud void pointers
        if rec['value'] == address and rec['type'].target().code != gdb.TYPE_CODE_VOID:
            return rec
    return None

# recursively finds all pointers
def find_pointers(memory):
    return find_pointers_rec(memory['heap']) + \
        [pointer for frame in memory['stack'] for pointer in find_pointers_rec(frame)]

def find_pointers_rec(rec):
    if rec['kind'] in ['frame', 'array', 'struct']:
        return [pointer for rec in rec['values'] for pointer in find_pointers_rec(rec)]
    elif rec['kind'] == 'pointer':
        return [rec]
    else:
        return []

def format_pointer(val):
    return hex(int(val)).replace('0x',"0x") if val is not None else ""

def rec_of_heap():
    # we return a 'frame' rec
    rec = {
        'kind': 'frame',
        'name': 'Heap',
        'fields': [],
        'values': [],
    }

    # get the linked list from watch_heap.c
    try:
        heap_node_ptr = gdb.parse_and_eval("heap_contents")['next']
    except:
        raise Exception(
            "Heap information not found.\n"
            "You need to load visualize-c-memory.so by setting the environment variable\n"
            "     LD_PRELOAD=<path-to>/visualize-c-memory.so\n"
            "_or_ link your program with visualize-c-memory.c"
        )
    #当堆区不为空,即heap_contents->next !=null
    while int(heap_node_ptr) != 0:
        # read node from the linked list
        heap_node = heap_node_ptr.dereference()
        pointer = heap_node['pointer']
        size = int(heap_node['size'])
        source = chr(heap_node['source'])
        heap_node_ptr = heap_node['next']

        # for the moment we have no type information, so we just create an 'untyped' record
        rec['fields'].append(f"{'malloc' if source == 'm' else 'realloc' if source == 'r' else 'calloc'}({size})")
        rec['values'].append({
            'name': " ",        # space to avoid errors
            'value': "?",
            'size': size,
            'address': format_pointer(pointer),
            'area': 'heap',
            'kind': 'untyped',
        })

    # the linked list contains the heap contents in reverse order
    rec['fields'].reverse()
    rec['values'].reverse()

    return rec

def infer_heap_types(memory):
    for i,rec in enumerate(memory['heap']['values']):
        if rec['kind'] != 'untyped':
            continue

        incoming_pointer = lookup_pointer(rec['address'], memory)
        if incoming_pointer is None:
            continue

        type = incoming_pointer['type']
        if type.target().code == gdb.TYPE_CODE_VOID:
            continue        # void pointer, not useful

        if type.target().sizeof == 0:
            # pointer to incomplete struct, just add the type name to the "?" value
            code_name = 'struct ' if type.target().code == gdb.TYPE_CODE_STRUCT else \
                        'union '  if type.target().code == gdb.TYPE_CODE_UNION  else ''
            rec['value'] = f'? ({code_name}{type.target().name})'
            continue

        # we use the type information to get a typed value, then
        # replace the heap rec with a new one obtained from the typed value
        n = int(rec['size'] / type.target().sizeof)
        if n > 1:
            # the malloced space is larger than the pointer's target type, most likely this is used as an array
            # we treat the pointer as a pointer to array
            type = type.target().array(n-1).pointer()

        value = gdb.Value(int(rec['address'], 16)).cast(type).dereference()
        memory['heap']['values'][i] = rec_of_value(value, 'heap')

        # the new value might itself contain pointers which can be used to
        # type records we already processed. So re-start frrom scratch
        return infer_heap_types(memory)

def recs_of_stack():
    res = []
    frame = gdb.newest_frame()
    while frame is not None:
        res.append(rec_of_frame(frame))
        frame = frame.older()

    res.reverse()
    return res

def rec_of_frame(frame):
    #frame表示当前帧
    # we want blocks in reverse order, but symbols within the block in the correct order!
    blocks = [frame.block()]

    while blocks[0].function is None:
        blocks.insert(0, blocks[0].superblock)
    #定义rec字典,收集当前帧的信息,包括function().name,
    #收集的局部变量和参数列表均是symb,symb.name即这些名称,symb.value
    rec = {
        'kind': 'frame',
        'name': frame.function().name,
        'fields': [],
        'values': [],
    }
    for block in blocks:
        for symb in block:
            # avoid "weird" symbols, eg labels
            if not (symb.is_variable or symb.is_argument or symb.is_function or symb.is_constant):
                continue

            var = symb.name
            rec['fields'].append(var)
            rec['values'].append(rec_of_value(symb.value(frame), 'stack'))

    return rec
# 这是一个用于将GDB值转换为可读格式的函数。
# 它将值转换为一个Python字典，其中包含有关该值的有用信息，例如类型、大小和地址等。
# 在这个函数中，它检查值的类型代码，
# 如果是数组，则迭代数组的元素并递归调用自身，将结果存储在'rec'字典的'values'键下。
# 如果是结构体，则类似地迭代其字段并递归调用自身，将结果存储在'rec'字典的'fields'和'values'键下。
# 对于指针和函数指针，它将它们视为标量值并将它们的十六进制值转换为字符串。
# 最后，对于其他类型的值，
# 它将尝试使用'format_string()'方法将值转换为字符串，并将结果存储在'rec'字典的'value'键下。
def rec_of_value(value, area):
    type = value.type.strip_typedefs()
    rec = {
        'size': type.sizeof,
        'address': format_pointer(value.address),
        'type': type,
        'area': area,
    }

    if type.code == gdb.TYPE_CODE_ARRAY:
        # stack arrays of dynamic length (eg int foo[n]) might have huge size before the
        # initialization code runs! In this case replace type with one of size 0
        # 初始化代码运行之前,堆栈数组也许非常巨大,在检测到这种情况时将数组的大小设为-1,即未知
        if int(type.sizeof) > 1000:
            type = type.target().array(-1)

        array_size = int(type.sizeof / type.target().sizeof)

        rec['values'] = [rec_of_value(value[i], area) for i in range(array_size)]
        rec['kind'] = 'array'

    elif type.code == gdb.TYPE_CODE_STRUCT:
        rec['fields'] = [field.name for field in type.fields()]
        #递归的得到结构体里的所有字段的值
        rec['values'] = [rec_of_value(value[field], area) for field in type.fields()]
        rec['kind'] = 'struct'

    else:
        # treat function pointers as scalar values
        #判断当前变量是否为指针,是否为函数指针
        is_pointer = (type.code == gdb.TYPE_CODE_PTR)
        func_pointer = (is_pointer and type.target().code == gdb.TYPE_CODE_FUNC)

        if is_pointer and not func_pointer:
            rec['value'] = format_pointer(value)
            rec['kind'] = 'pointer'
        else:
            try:
                rec['value'] = html.escape(value.format_string())
            except:
                rec['value'] = '?'
            rec['kind'] = 'other'
            if func_pointer:
                rec['value'] = rec['value'].replace("0x", "").replace(" ", "<br/>")

    return rec


