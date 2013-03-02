import sys, imp
from . import model, ffiplatform


class VCPythonEngine(object):
    _class_key = 'x'
    _gen_python_module = True

    def __init__(self, verifier):
        self.verifier = verifier
        self.ffi = verifier.ffi
        self._struct_pending_verification = {}

    def patch_extension_kwds(self, kwds):
        pass

    def collect_types(self):
        self._typesdict = {}
        self._generate("collecttype")

    def _prnt(self, what=''):
        self._f.write(what + '\n')

    def _gettypenum(self, type):
        # a KeyError here is a bug.  please report it! :-)
        return self._typesdict[type]

    def _do_collect_type(self, tp):
        if ((not isinstance(tp, model.PrimitiveType)
             or tp.name == 'long double')
                and tp not in self._typesdict):
            num = len(self._typesdict)
            self._typesdict[tp] = num

    def write_source_to_f(self):
        self.collect_types()
        #
        # The new module will have a _cffi_setup() function that receives
        # objects from the ffi world, and that calls some setup code in
        # the module.  This setup code is split in several independent
        # functions, e.g. one per constant.  The functions are "chained"
        # by ending in a tail call to each other.
        #
        # This is further split in two chained lists, depending on if we
        # can do it at import-time or if we must wait for _cffi_setup() to
        # provide us with the <ctype> objects.  This is needed because we
        # need the values of the enum constants in order to build the
        # <ctype 'enum'> that we may have to pass to _cffi_setup().
        #
        # The following two 'chained_list_constants' items contains
        # the head of these two chained lists, as a string that gives the
        # call to do, if any.
        self._chained_list_constants = ['0', '0']
        #
        prnt = self._prnt
        # first paste some standard set of lines that are mostly '#define'
        prnt(cffimod_header)
        prnt()
        # then paste the C source given by the user, verbatim.
        prnt(self.verifier.preamble)
        prnt()
        #
        # call generate_cpy_xxx_decl(), for every xxx found from
        # ffi._parser._declarations.  This generates all the functions.
        self._generate("decl")
        #
        # implement the function _cffi_setup_custom() as calling the
        # head of the chained list.
        self._generate_setup_custom()
        prnt()
        #
        # produce the method table, including the entries for the
        # generated Python->C function wrappers, which are done
        # by generate_cpy_function_method().
        prnt('static PyMethodDef _cffi_methods[] = {')
        self._generate("method")
        prnt('  {"_cffi_setup", _cffi_setup, METH_VARARGS},')
        prnt('  {NULL, NULL}    /* Sentinel */')
        prnt('};')
        prnt()
        #
        # standard init.
        modname = self.verifier.get_module_name()
        if sys.version_info >= (3,):
            prnt('static struct PyModuleDef _cffi_module_def = {')
            prnt('  PyModuleDef_HEAD_INIT,')
            prnt('  "%s",' % modname)
            prnt('  NULL,')
            prnt('  -1,')
            prnt('  _cffi_methods,')
            prnt('  NULL, NULL, NULL, NULL')
            prnt('};')
            prnt()
            initname = 'PyInit_%s' % modname
            createmod = 'PyModule_Create(&_cffi_module_def)'
            errorcase = 'return NULL'
            finalreturn = 'return lib'
        else:
            initname = 'init%s' % modname
            createmod = 'Py_InitModule("%s", _cffi_methods)' % modname
            errorcase = 'return'
            finalreturn = 'return'
        prnt('PyMODINIT_FUNC')
        prnt('%s(void)' % initname)
        prnt('{')
        prnt('  PyObject *lib;')
        prnt('  lib = %s;' % createmod)
        prnt('  if (lib == NULL || %s < 0)' % (
            self._chained_list_constants[False],))
        prnt('    %s;' % errorcase)
        prnt('  _cffi_init();')
        prnt('  %s;' % finalreturn)
        prnt('}')

    def load_library(self):
        # XXX review all usages of 'self' here!
        # import it as a new extension module
        try:
            module = imp.load_dynamic(self.verifier.get_module_name(),
                                      self.verifier.modulefilename)
        except ImportError as e:
            error = "importing %r: %s" % (self.verifier.modulefilename, e)
            raise ffiplatform.VerificationError(error)
        #
        # call loading_cpy_struct() to get the struct layout inferred by
        # the C compiler
        self._load(module, 'loading')
        #
        # the C code will need the <ctype> objects.  Collect them in
        # order in a list.
        revmapping = dict([(value, key)
                           for (key, value) in self._typesdict.items()])
        lst = [revmapping[i] for i in range(len(revmapping))]
        lst = list(map(self.ffi._get_cached_btype, lst))
        #
        # build the FFILibrary class and instance and call _cffi_setup().
        # this will set up some fields like '_cffi_types', and only then
        # it will invoke the chained list of functions that will really
        # build (notably) the constant objects, as <cdata> if they are
        # pointers, and store them as attributes on the 'library' object.
        class FFILibrary(object):
            _cffi_python_module = module
            _cffi_ffi = self.ffi
        library = FFILibrary()
        module._cffi_setup(lst, ffiplatform.VerificationError, library)
        #
        # finally, call the loaded_cpy_xxx() functions.  This will perform
        # the final adjustments, like copying the Python->C wrapper
        # functions from the module to the 'library' object, and setting
        # up the FFILibrary class with properties for the global C variables.
        self._load(module, 'loaded', library=library)
        return library

    def _get_declarations(self):
        return sorted(self.ffi._parser._declarations.items())

    def _generate(self, step_name):
        for name, tp in self._get_declarations():
            kind, realname = name.split(' ', 1)
            try:
                method = getattr(self, '_generate_cpy_%s_%s' % (kind,
                                                                step_name))
            except AttributeError:
                raise ffiplatform.VerificationError(
                    "not implemented in verify(): %r" % name)
            try:
                method(tp, realname)
            except Exception as e:
                model.attach_exception_info(e, name)
                raise

    def _load(self, module, step_name, **kwds):
        for name, tp in self._get_declarations():
            kind, realname = name.split(' ', 1)
            method = getattr(self, '_%s_cpy_%s' % (step_name, kind))
            try:
                method(tp, realname, module, **kwds)
            except Exception as e:
                model.attach_exception_info(e, name)
                raise

    def _generate_nothing(self, tp, name):
        pass

    def _loaded_noop(self, tp, name, module, **kwds):
        pass

    # ----------

    def _convert_funcarg_to_c(self, tp, fromvar, tovar, errcode):
        extraarg = ''
        if isinstance(tp, model.PrimitiveType):
            if tp.is_integer_type() and tp.name != '_Bool':
                if tp.is_signed_type():
                    converter = '_cffi_to_c_SIGNED'
                else:
                    converter = '_cffi_to_c_UNSIGNED'
                extraarg = ', %s' % tp.name
            else:
                converter = '_cffi_to_c_%s' % (tp.name.replace(' ', '_'),)
            errvalue = '-1'
        #
        elif isinstance(tp, model.PointerType):
            self._convert_funcarg_to_c_ptr_or_array(tp, fromvar,
                                                    tovar, errcode)
            return
        #
        elif isinstance(tp, (model.StructOrUnion, model.EnumType)):
            # a struct (not a struct pointer) as a function argument
            self._prnt('  if (_cffi_to_c((char *)&%s, _cffi_type(%d), %s) < 0)'
                      % (tovar, self._gettypenum(tp), fromvar))
            self._prnt('    %s;' % errcode)
            return
        #
        elif isinstance(tp, model.FunctionPtrType):
            converter = '(%s)_cffi_to_c_pointer' % tp.get_c_name('')
            extraarg = ', _cffi_type(%d)' % self._gettypenum(tp)
            errvalue = 'NULL'
        #
        else:
            raise NotImplementedError(tp)
        #
        self._prnt('  %s = %s(%s%s);' % (tovar, converter, fromvar, extraarg))
        self._prnt('  if (%s == (%s)%s && PyErr_Occurred())' % (
            tovar, tp.get_c_name(''), errvalue))
        self._prnt('    %s;' % errcode)

    def _extra_local_variables(self, tp, localvars):
        if isinstance(tp, model.PointerType):
            localvars.add('Py_ssize_t datasize')

    def _convert_funcarg_to_c_ptr_or_array(self, tp, fromvar, tovar, errcode):
        self._prnt('  datasize = _cffi_prepare_pointer_call_argument(')
        self._prnt('      _cffi_type(%d), %s, (char **)&%s);' % (
            self._gettypenum(tp), fromvar, tovar))
        self._prnt('  if (datasize != 0) {')
        self._prnt('    if (datasize < 0)')
        self._prnt('      %s;' % errcode)
        self._prnt('    %s = alloca(datasize);' % (tovar,))
        self._prnt('    memset((void *)%s, 0, datasize);' % (tovar,))
        self._prnt('    if (_cffi_convert_array_from_object('
                   '(char *)%s, _cffi_type(%d), %s) < 0)' % (
            tovar, self._gettypenum(tp), fromvar))
        self._prnt('      %s;' % errcode)
        self._prnt('  }')

    def _convert_expr_from_c(self, tp, var, context):
        if isinstance(tp, model.PrimitiveType):
            if tp.is_integer_type():
                if tp.is_signed_type():
                    return '_cffi_from_c_SIGNED(%s, %s)' % (var, tp.name)
                else:
                    return '_cffi_from_c_UNSIGNED(%s, %s)' % (var, tp.name)
            elif tp.name != 'long double':
                return '_cffi_from_c_%s(%s)' % (tp.name.replace(' ', '_'), var)
            else:
                return '_cffi_from_c_deref((char *)&%s, _cffi_type(%d))' % (
                    var, self._gettypenum(tp))
        elif isinstance(tp, (model.PointerType, model.FunctionPtrType)):
            return '_cffi_from_c_pointer((char *)%s, _cffi_type(%d))' % (
                var, self._gettypenum(tp))
        elif isinstance(tp, model.ArrayType):
            return '_cffi_from_c_deref((char *)%s, _cffi_type(%d))' % (
                var, self._gettypenum(tp))
        elif isinstance(tp, model.StructType):
            if tp.fldnames is None:
                raise TypeError("'%s' is used as %s, but is opaque" % (
                    tp._get_c_name(''), context))
            return '_cffi_from_c_struct((char *)&%s, _cffi_type(%d))' % (
                var, self._gettypenum(tp))
        elif isinstance(tp, model.EnumType):
            return '_cffi_from_c_deref((char *)&%s, _cffi_type(%d))' % (
                var, self._gettypenum(tp))
        else:
            raise NotImplementedError(tp)

    # ----------
    # typedefs: generates no code so far

    _generate_cpy_typedef_collecttype = _generate_nothing
    _generate_cpy_typedef_decl   = _generate_nothing
    _generate_cpy_typedef_method = _generate_nothing
    _loading_cpy_typedef         = _loaded_noop
    _loaded_cpy_typedef          = _loaded_noop

    # ----------
    # function declarations

    def _generate_cpy_function_collecttype(self, tp, name):
        assert isinstance(tp, model.FunctionPtrType)
        if tp.ellipsis:
            self._do_collect_type(tp)
        else:
            for type in tp.args:
                self._do_collect_type(type)
            self._do_collect_type(tp.result)

    def _generate_cpy_function_decl(self, tp, name):
        assert isinstance(tp, model.FunctionPtrType)
        if tp.ellipsis:
            # cannot support vararg functions better than this: check for its
            # exact type (including the fixed arguments), and build it as a
            # constant function pointer (no CPython wrapper)
            self._generate_cpy_const(False, name, tp)
            return
        prnt = self._prnt
        numargs = len(tp.args)
        if numargs == 0:
            argname = 'no_arg'
        elif numargs == 1:
            argname = 'arg0'
        else:
            argname = 'args'
        prnt('static PyObject *')
        prnt('_cffi_f_%s(PyObject *self, PyObject *%s)' % (name, argname))
        prnt('{')
        #
        context = 'argument of %s' % name
        for i, type in enumerate(tp.args):
            prnt('  %s;' % type.get_c_name(' x%d' % i, context))
        #
        localvars = set()
        for type in tp.args:
            self._extra_local_variables(type, localvars)
        for decl in localvars:
            prnt('  %s;' % (decl,))
        #
        if not isinstance(tp.result, model.VoidType):
            result_code = 'result = '
            context = 'result of %s' % name
            prnt('  %s;' % tp.result.get_c_name(' result', context))
        else:
            result_code = ''
        #
        if len(tp.args) > 1:
            rng = range(len(tp.args))
            for i in rng:
                prnt('  PyObject *arg%d;' % i)
            prnt()
            prnt('  if (!PyArg_ParseTuple(args, "%s:%s", %s))' % (
                'O' * numargs, name, ', '.join(['&arg%d' % i for i in rng])))
            prnt('    return NULL;')
        prnt()
        #
        for i, type in enumerate(tp.args):
            self._convert_funcarg_to_c(type, 'arg%d' % i, 'x%d' % i,
                                       'return NULL')
            prnt()
        #
        prnt('  Py_BEGIN_ALLOW_THREADS')
        prnt('  _cffi_restore_errno();')
        prnt('  { %s%s(%s); }' % (
            result_code, name,
            ', '.join(['x%d' % i for i in range(len(tp.args))])))
        prnt('  _cffi_save_errno();')
        prnt('  Py_END_ALLOW_THREADS')
        prnt()
        #
        if result_code:
            prnt('  return %s;' %
                 self._convert_expr_from_c(tp.result, 'result', 'result type'))
        else:
            prnt('  Py_INCREF(Py_None);')
            prnt('  return Py_None;')
        prnt('}')
        prnt()

    def _generate_cpy_function_method(self, tp, name):
        if tp.ellipsis:
            return
        numargs = len(tp.args)
        if numargs == 0:
            meth = 'METH_NOARGS'
        elif numargs == 1:
            meth = 'METH_O'
        else:
            meth = 'METH_VARARGS'
        self._prnt('  {"%s", _cffi_f_%s, %s},' % (name, name, meth))

    _loading_cpy_function = _loaded_noop

    def _loaded_cpy_function(self, tp, name, module, library):
        if tp.ellipsis:
            return
        setattr(library, name, getattr(module, name))

    # ----------
    # named structs

    _generate_cpy_struct_collecttype = _generate_nothing
    def _generate_cpy_struct_decl(self, tp, name):
        assert name == tp.name
        self._generate_struct_or_union_decl(tp, 'struct', name)
    def _generate_cpy_struct_method(self, tp, name):
        self._generate_struct_or_union_method(tp, 'struct', name)
    def _loading_cpy_struct(self, tp, name, module):
        self._loading_struct_or_union(tp, 'struct', name, module)
    def _loaded_cpy_struct(self, tp, name, module, **kwds):
        self._loaded_struct_or_union(tp)

    _generate_cpy_union_collecttype = _generate_nothing
    def _generate_cpy_union_decl(self, tp, name):
        assert name == tp.name
        self._generate_struct_or_union_decl(tp, 'union', name)
    def _generate_cpy_union_method(self, tp, name):
        self._generate_struct_or_union_method(tp, 'union', name)
    def _loading_cpy_union(self, tp, name, module):
        self._loading_struct_or_union(tp, 'union', name, module)
    def _loaded_cpy_union(self, tp, name, module, **kwds):
        self._loaded_struct_or_union(tp)

    def _generate_struct_or_union_decl(self, tp, prefix, name):
        if tp.fldnames is None:
            return     # nothing to do with opaque structs
        checkfuncname = '_cffi_check_%s_%s' % (prefix, name)
        layoutfuncname = '_cffi_layout_%s_%s' % (prefix, name)
        cname = ('%s %s' % (prefix, name)).strip()
        #
        prnt = self._prnt
        prnt('static void %s(%s *p)' % (checkfuncname, cname))
        prnt('{')
        prnt('  /* only to generate compile-time warnings or errors */')
        for fname, ftype, _ in tp.enumfields():
            if (isinstance(ftype, model.PrimitiveType)
                and ftype.is_integer_type()):
                # accept all integers, but complain on float or double
                prnt('  (void)((p->%s) << 1);' % fname)
            else:
                # only accept exactly the type declared.
                try:
                    prnt('  { %s = &p->%s; (void)tmp; }' % (
                        ftype.get_c_name('*tmp', 'field %r'%fname), fname))
                except ffiplatform.VerificationError as e:
                    prnt('  /* %s */' % str(e))   # cannot verify it, ignore
        prnt('}')
        prnt('static PyObject *')
        prnt('%s(PyObject *self, PyObject *noarg)' % (layoutfuncname,))
        prnt('{')
        prnt('  struct _cffi_aligncheck { char x; %s y; };' % cname)
        prnt('  static Py_ssize_t nums[] = {')
        prnt('    sizeof(%s),' % cname)
        prnt('    offsetof(struct _cffi_aligncheck, y),')
        for fname, _, fbitsize in tp.enumfields():
            if fbitsize >= 0:
                continue      # xxx ignore fbitsize for now
            prnt('    offsetof(%s, %s),' % (cname, fname))
            prnt('    sizeof(((%s *)0)->%s),' % (cname, fname))
        prnt('    -1')
        prnt('  };')
        prnt('  return _cffi_get_struct_layout(nums);')
        prnt('  /* the next line is not executed, but compiled */')
        prnt('  %s(0);' % (checkfuncname,))
        prnt('}')
        prnt()

    def _generate_struct_or_union_method(self, tp, prefix, name):
        if tp.fldnames is None:
            return     # nothing to do with opaque structs
        layoutfuncname = '_cffi_layout_%s_%s' % (prefix, name)
        self._prnt('  {"%s", %s, METH_NOARGS},' % (layoutfuncname,
                                                   layoutfuncname))

    def _loading_struct_or_union(self, tp, prefix, name, module):
        if tp.fldnames is None:
            return     # nothing to do with opaque structs
        layoutfuncname = '_cffi_layout_%s_%s' % (prefix, name)
        #
        function = getattr(module, layoutfuncname)
        layout = function()
        if isinstance(tp, model.StructType) and tp.partial:
            # use the function()'s sizes and offsets to guide the
            # layout of the struct
            totalsize = layout[0]
            totalalignment = layout[1]
            fieldofs = layout[2::2]
            fieldsize = layout[3::2]
            tp.force_flatten()
            assert len(fieldofs) == len(fieldsize) == len(tp.fldnames)
            tp.fixedlayout = fieldofs, fieldsize, totalsize, totalalignment
        else:
            cname = ('%s %s' % (prefix, name)).strip()
            self._struct_pending_verification[tp] = layout, cname

    def _loaded_struct_or_union(self, tp):
        if tp.fldnames is None:
            return     # nothing to do with opaque structs
        self.ffi._get_cached_btype(tp)   # force 'fixedlayout' to be considered

        if tp in self._struct_pending_verification:
            # check that the layout sizes and offsets match the real ones
            def check(realvalue, expectedvalue, msg):
                if realvalue != expectedvalue:
                    raise ffiplatform.VerificationError(
                        "%s (we have %d, but C compiler says %d)"
                        % (msg, expectedvalue, realvalue))
            ffi = self.ffi
            BStruct = ffi._get_cached_btype(tp)
            layout, cname = self._struct_pending_verification.pop(tp)
            check(layout[0], ffi.sizeof(BStruct), "wrong total size")
            check(layout[1], ffi.alignof(BStruct), "wrong total alignment")
            i = 2
            for fname, ftype, fbitsize in tp.enumfields():
                if fbitsize >= 0:
                    continue        # xxx ignore fbitsize for now
                check(layout[i], ffi.offsetof(BStruct, fname),
                      "wrong offset for field %r" % (fname,))
                BField = ffi._get_cached_btype(ftype)
                check(layout[i+1], ffi.sizeof(BField),
                      "wrong size for field %r" % (fname,))
                i += 2
            assert i == len(layout)

    # ----------
    # 'anonymous' declarations.  These are produced for anonymous structs
    # or unions; the 'name' is obtained by a typedef.

    _generate_cpy_anonymous_collecttype = _generate_nothing

    def _generate_cpy_anonymous_decl(self, tp, name):
        if isinstance(tp, model.EnumType):
            self._generate_cpy_enum_decl(tp, name, '')
        else:
            self._generate_struct_or_union_decl(tp, '', name)

    def _generate_cpy_anonymous_method(self, tp, name):
        if not isinstance(tp, model.EnumType):
            self._generate_struct_or_union_method(tp, '', name)

    def _loading_cpy_anonymous(self, tp, name, module):
        if isinstance(tp, model.EnumType):
            self._loading_cpy_enum(tp, name, module)
        else:
            self._loading_struct_or_union(tp, '', name, module)

    def _loaded_cpy_anonymous(self, tp, name, module, **kwds):
        if isinstance(tp, model.EnumType):
            self._loaded_cpy_enum(tp, name, module, **kwds)
        else:
            self._loaded_struct_or_union(tp)

    # ----------
    # constants, likely declared with '#define'

    def _generate_cpy_const(self, is_int, name, tp=None, category='const',
                            vartp=None, delayed=True):
        prnt = self._prnt
        funcname = '_cffi_%s_%s' % (category, name)
        prnt('static int %s(PyObject *lib)' % funcname)
        prnt('{')
        prnt('  PyObject *o;')
        prnt('  int res;')
        if not is_int:
            prnt('  %s;' % (vartp or tp).get_c_name(' i', name))
        else:
            assert category == 'const'
        #
        if not is_int:
            if category == 'var':
                realexpr = '&' + name
            else:
                realexpr = name
            prnt('  i = (%s);' % (realexpr,))
            prnt('  o = %s;' % (self._convert_expr_from_c(tp, 'i',
                                                          'variable type'),))
            assert delayed
        else:
            prnt('  if (LONG_MIN <= (%s) && (%s) <= LONG_MAX)' % (name, name))
            prnt('    o = PyInt_FromLong((long)(%s));' % (name,))
            prnt('  else if ((%s) <= 0)' % (name,))
            prnt('    o = PyLong_FromLongLong((long long)(%s));' % (name,))
            prnt('  else')
            prnt('    o = PyLong_FromUnsignedLongLong('
                 '(unsigned long long)(%s));' % (name,))
        prnt('  if (o == NULL)')
        prnt('    return -1;')
        prnt('  res = PyObject_SetAttrString(lib, "%s", o);' % name)
        prnt('  Py_DECREF(o);')
        prnt('  if (res < 0)')
        prnt('    return -1;')
        prnt('  return %s;' % self._chained_list_constants[delayed])
        self._chained_list_constants[delayed] = funcname + '(lib)'
        prnt('}')
        prnt()

    def _generate_cpy_constant_collecttype(self, tp, name):
        is_int = isinstance(tp, model.PrimitiveType) and tp.is_integer_type()
        if not is_int:
            self._do_collect_type(tp)

    def _generate_cpy_constant_decl(self, tp, name):
        is_int = isinstance(tp, model.PrimitiveType) and tp.is_integer_type()
        self._generate_cpy_const(is_int, name, tp)

    _generate_cpy_constant_method = _generate_nothing
    _loading_cpy_constant = _loaded_noop
    _loaded_cpy_constant  = _loaded_noop

    # ----------
    # enums

    def _generate_cpy_enum_decl(self, tp, name, prefix='enum'):
        if tp.partial:
            for enumerator in tp.enumerators:
                self._generate_cpy_const(True, enumerator, delayed=False)
            return
        #
        funcname = '_cffi_e_%s_%s' % (prefix, name)
        prnt = self._prnt
        prnt('static int %s(PyObject *lib)' % funcname)
        prnt('{')
        for enumerator, enumvalue in zip(tp.enumerators, tp.enumvalues):
            prnt('  if (%s != %d) {' % (enumerator, enumvalue))
            prnt('    PyErr_Format(_cffi_VerificationError,')
            prnt('                 "enum %s: %s has the real value %d, '
                 'not %d",')
            prnt('                 "%s", "%s", (int)%s, %d);' % (
                name, enumerator, enumerator, enumvalue))
            prnt('    return -1;')
            prnt('  }')
        prnt('  return %s;' % self._chained_list_constants[True])
        self._chained_list_constants[True] = funcname + '(lib)'
        prnt('}')
        prnt()

    _generate_cpy_enum_collecttype = _generate_nothing
    _generate_cpy_enum_method = _generate_nothing

    def _loading_cpy_enum(self, tp, name, module):
        if tp.partial:
            enumvalues = [getattr(module, enumerator)
                          for enumerator in tp.enumerators]
            tp.enumvalues = tuple(enumvalues)
            tp.partial_resolved = True

    def _loaded_cpy_enum(self, tp, name, module, library):
        for enumerator, enumvalue in zip(tp.enumerators, tp.enumvalues):
            setattr(library, enumerator, enumvalue)

    # ----------
    # macros: for now only for integers

    def _generate_cpy_macro_decl(self, tp, name):
        assert tp == '...'
        self._generate_cpy_const(True, name)

    _generate_cpy_macro_collecttype = _generate_nothing
    _generate_cpy_macro_method = _generate_nothing
    _loading_cpy_macro = _loaded_noop
    _loaded_cpy_macro  = _loaded_noop

    # ----------
    # global variables

    def _generate_cpy_variable_collecttype(self, tp, name):
        if isinstance(tp, model.ArrayType):
            self._do_collect_type(tp)
        else:
            tp_ptr = model.PointerType(tp)
            self._do_collect_type(tp_ptr)

    def _generate_cpy_variable_decl(self, tp, name):
        if isinstance(tp, model.ArrayType):
            tp_ptr = model.PointerType(tp.item)
            self._generate_cpy_const(False, name, tp, vartp=tp_ptr)
        else:
            tp_ptr = model.PointerType(tp)
            self._generate_cpy_const(False, name, tp_ptr, category='var')

    _generate_cpy_variable_method = _generate_nothing
    _loading_cpy_variable = _loaded_noop

    def _loaded_cpy_variable(self, tp, name, module, library):
        if isinstance(tp, model.ArrayType):   # int a[5] is "constant" in the
            return                            # sense that "a=..." is forbidden
        # remove ptr=<cdata 'int *'> from the library instance, and replace
        # it by a property on the class, which reads/writes into ptr[0].
        ptr = getattr(library, name)
        delattr(library, name)
        def getter(library):
            return ptr[0]
        def setter(library, value):
            ptr[0] = value
        setattr(library.__class__, name, property(getter, setter))

    # ----------

    def _generate_setup_custom(self):
        prnt = self._prnt
        prnt('static PyObject *_cffi_setup_custom(PyObject *lib)')
        prnt('{')
        prnt('  if (%s < 0)' % self._chained_list_constants[True])
        prnt('    return NULL;')
        prnt('  Py_INCREF(Py_None);')
        prnt('  return Py_None;')
        prnt('}')

cffimod_header = r'''
#include <Python.h>
#include <stddef.h>

#ifdef MS_WIN32
#include <malloc.h>   /* for alloca() */
typedef __int8 int8_t;
typedef __int16 int16_t;
typedef __int32 int32_t;
typedef __int64 int64_t;
typedef unsigned __int8 uint8_t;
typedef unsigned __int16 uint16_t;
typedef unsigned __int32 uint32_t;
typedef unsigned __int64 uint64_t;
typedef unsigned char _Bool;
#endif

#if PY_MAJOR_VERSION < 3
# undef PyCapsule_CheckExact
# undef PyCapsule_GetPointer
# define PyCapsule_CheckExact(capsule) (PyCObject_Check(capsule))
# define PyCapsule_GetPointer(capsule, name) \
    (PyCObject_AsVoidPtr(capsule))
#endif

#if PY_MAJOR_VERSION >= 3
# define PyInt_FromLong PyLong_FromLong
#endif

#define _cffi_from_c_double PyFloat_FromDouble
#define _cffi_from_c_float PyFloat_FromDouble
#define _cffi_from_c_long PyInt_FromLong
#define _cffi_from_c_ulong PyLong_FromUnsignedLong
#define _cffi_from_c_longlong PyLong_FromLongLong
#define _cffi_from_c_ulonglong PyLong_FromUnsignedLongLong

#define _cffi_to_c_double PyFloat_AsDouble
#define _cffi_to_c_float PyFloat_AsDouble

#define _cffi_from_c_SIGNED(x, type)                                     \
    (sizeof(type) <= sizeof(long) ? PyInt_FromLong(x) :                  \
                                    PyLong_FromLongLong(x))
#define _cffi_from_c_UNSIGNED(x, type)                                   \
    (sizeof(type) < sizeof(long) ? PyInt_FromLong(x) :                   \
     sizeof(type) == sizeof(long) ? PyLong_FromUnsignedLong(x) :         \
                                    PyLong_FromUnsignedLongLong(x))

#define _cffi_to_c_SIGNED(o, type)                                       \
    (sizeof(type) == 1 ? _cffi_to_c_i8(o) :                              \
     sizeof(type) == 2 ? _cffi_to_c_i16(o) :                             \
     sizeof(type) == 4 ? _cffi_to_c_i32(o) :                             \
     sizeof(type) == 8 ? _cffi_to_c_i64(o) :                             \
     (Py_FatalError("unsupported size for type " #type), 0))
#define _cffi_to_c_UNSIGNED(o, type)                                     \
    (sizeof(type) == 1 ? _cffi_to_c_u8(o) :                              \
     sizeof(type) == 2 ? _cffi_to_c_u16(o) :                             \
     sizeof(type) == 4 ? _cffi_to_c_u32(o) :                             \
     sizeof(type) == 8 ? _cffi_to_c_u64(o) :                             \
     (Py_FatalError("unsupported size for type " #type), 0))

#define _cffi_to_c_i8                                                    \
                 ((int(*)(PyObject *))_cffi_exports[1])
#define _cffi_to_c_u8                                                    \
                 ((int(*)(PyObject *))_cffi_exports[2])
#define _cffi_to_c_i16                                                   \
                 ((int(*)(PyObject *))_cffi_exports[3])
#define _cffi_to_c_u16                                                   \
                 ((int(*)(PyObject *))_cffi_exports[4])
#define _cffi_to_c_i32                                                   \
                 ((int(*)(PyObject *))_cffi_exports[5])
#define _cffi_to_c_u32                                                   \
                 ((unsigned int(*)(PyObject *))_cffi_exports[6])
#define _cffi_to_c_i64                                                   \
                 ((long long(*)(PyObject *))_cffi_exports[7])
#define _cffi_to_c_u64                                                   \
                 ((unsigned long long(*)(PyObject *))_cffi_exports[8])
#define _cffi_to_c_char                                                  \
                 ((char(*)(PyObject *))_cffi_exports[9])
#define _cffi_from_c_pointer                                             \
    ((PyObject *(*)(char *, CTypeDescrObject *))_cffi_exports[10])
#define _cffi_to_c_pointer                                               \
    ((char *(*)(PyObject *, CTypeDescrObject *))_cffi_exports[11])
#define _cffi_get_struct_layout                                          \
    ((PyObject *(*)(Py_ssize_t[]))_cffi_exports[12])
#define _cffi_restore_errno                                              \
    ((void(*)(void))_cffi_exports[13])
#define _cffi_save_errno                                                 \
    ((void(*)(void))_cffi_exports[14])
#define _cffi_from_c_char                                                \
    ((PyObject *(*)(char))_cffi_exports[15])
#define _cffi_from_c_deref                                               \
    ((PyObject *(*)(char *, CTypeDescrObject *))_cffi_exports[16])
#define _cffi_to_c                                                       \
    ((int(*)(char *, CTypeDescrObject *, PyObject *))_cffi_exports[17])
#define _cffi_from_c_struct                                              \
    ((PyObject *(*)(char *, CTypeDescrObject *))_cffi_exports[18])
#define _cffi_to_c_wchar_t                                               \
    ((wchar_t(*)(PyObject *))_cffi_exports[19])
#define _cffi_from_c_wchar_t                                             \
    ((PyObject *(*)(wchar_t))_cffi_exports[20])
#define _cffi_to_c_long_double                                           \
    ((long double(*)(PyObject *))_cffi_exports[21])
#define _cffi_to_c__Bool                                                 \
    ((_Bool(*)(PyObject *))_cffi_exports[22])
#define _cffi_prepare_pointer_call_argument                              \
    ((Py_ssize_t(*)(CTypeDescrObject *, PyObject *, char **))_cffi_exports[23])
#define _cffi_convert_array_from_object                                  \
    ((int(*)(char *, CTypeDescrObject *, PyObject *))_cffi_exports[24])
#define _CFFI_NUM_EXPORTS 25

typedef struct _ctypedescr CTypeDescrObject;

static void *_cffi_exports[_CFFI_NUM_EXPORTS];
static PyObject *_cffi_types, *_cffi_VerificationError;

static PyObject *_cffi_setup_custom(PyObject *lib);   /* forward */

static PyObject *_cffi_setup(PyObject *self, PyObject *args)
{
    PyObject *library;
    if (!PyArg_ParseTuple(args, "OOO", &_cffi_types, &_cffi_VerificationError,
                                       &library))
        return NULL;
    Py_INCREF(_cffi_types);
    Py_INCREF(_cffi_VerificationError);
    return _cffi_setup_custom(library);
}

static void _cffi_init(void)
{
    PyObject *module = PyImport_ImportModule("_cffi_backend");
    PyObject *c_api_object;

    if (module == NULL)
        return;

    c_api_object = PyObject_GetAttrString(module, "_C_API");
    if (c_api_object == NULL)
        return;
    if (!PyCapsule_CheckExact(c_api_object)) {
        PyErr_SetNone(PyExc_ImportError);
        return;
    }
    memcpy(_cffi_exports, PyCapsule_GetPointer(c_api_object, "cffi"),
           _CFFI_NUM_EXPORTS * sizeof(void *));
}

#define _cffi_type(num) ((CTypeDescrObject *)PyList_GET_ITEM(_cffi_types, num))

/**********/
'''
