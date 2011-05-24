#!/usr/bin/env python
# -*- Mode: Python; tab-width: 4; indent-tabs-mode: nil; coding: utf-8; -*-
# vim:set ft=python ts=4 sw=4 sts=4 autoindent:

from __future__ import with_statement

'''
Functionality related to the annotation file format.

Author:     Pontus Stenetorp   <pontus is s u tokyo ac jp>
Version:    2010-01-25
'''

# TODO: Major re-work, cleaning up and conforming with new server paradigm

from logging import info as log_info
from codecs import open as codecs_open
from functools import partial
from os import utime
from time import time
from os.path import join as path_join
from os.path import basename

from common import ProtocolError
from config import WORK_DIR
from filelock import file_lock
from message import display_message

### Constants
# The only suffix we allow to write to, which is the joined annotation file
JOINED_ANN_FILE_SUFF = 'ann'
# These file suffixes indicate partial annotations that can not be written to
# since they depend on multiple files for completeness
PARTIAL_ANN_FILE_SUFF = ['a1', 'a2', 'co', 'rel']
TEXT_FILE_SUFFIX = 'txt'
###


class AnnotationLineSyntaxError(Exception):
    def __init__(self, line, line_num):
        self.line = line
        self.line_num = line_num

    def __str__(self):
        u'Syntax error on line %d: "%s"' % (self.line_num, self.line)

class IdedAnnotationLineSyntaxError(AnnotationLineSyntaxError):
    def __init__(self, id, line, line_num):
        AnnotationLineSyntaxError.__init__(self, line, line_num)
        self.id = id

    def __str__(self):
        u'Syntax error on line %d (id %s): "%s"' % (self.line_num, self.id, self.line)

class AnnotationNotFoundError(Exception):
    def __init__(self, id):
        self.id = id

    def __str__(self):
        return u'Could not find an annotation with id: %s' % (self.id, )

class AnnotationFileNotFoundError(ProtocolError):
    def __init__(self, fn):
        self.fn = fn

    def __str__(self):
        return u'Could not find any annotations for %s' % (self.fn, )

    def json(self, json_dic):
        json_dic['exception'] = 'annotationFileNotFound'
        return json_dic

class AnnotationTextFileNotFoundError(AnnotationFileNotFoundError):
    def __str__(self):
        return u'Could not read text file for %s' % (self.fn, )

class AnnotationsIsReadOnlyError(ProtocolError):
    def __init__(self):
        pass

    def json(self, json_dic):
        json_dic['exception'] = 'annotationIsReadOnly'
        return json_dic


class DuplicateAnnotationIdError(Exception):
    def __init__(self, id):
        self.id = id

    def __str__(self):
        return u'Encountered a duplicate of id: %s' % (self.id, )


class InvalidIdError(Exception):
    def __init__(self, id):
        self.id = id

    def __str__(self):
        return u'Invalid id: %s' % (self.id, )


class DependingAnnotationDeleteError(Exception):
    def __init__(self, target, dependants):
        self.target = target
        self.dependants = dependants

    def __str__(self):
        return u'%s can not be deleted due to depending annotations %s' % (
                self.target, ",".join([unicode(d) for d in self.dependants]))

    def html_error_str(self, response=None):
        return u'''
        Annotation:
        <br/>
        %s
        <br/>
        Has depending annotations attached to it:
        <br/>
        %s
        ''' % (self.target, ",".join([unicode(d) for d in self.dependants]))

# Open function that enforces strict, utf-8
# TODO: Could have another wrapping layer raising an appropriate ProtocolError
open_textfile = partial(codecs_open, errors='strict', encoding='utf8')

def __split_annotation_id(id):
    import re
    m = re.match(r'^([A-Za-z]|#)([0-9]+)(.*?)$', id)
    if m is None:
        raise InvalidIdError(id)
    pre, num_str, suf = m.groups()
    return pre, num_str, suf

def annotation_id_prefix(id):
    try:
        return id[0]
    except:
        raise InvalidIdError(id)

def annotation_id_number(id):
    return __split_annotation_id(id)[1]

class Annotations(object):
    """
    Basic annotation storage. Not concerned with conformity of
    annotations to text; can be created without access to the
    text file to which the annotations apply.
    """

    def get_document(self):
        return self._document
    
    def _select_input_files(self, document):
        """
        Given a document name (path), returns a list of the names of
        specific annotation files relevant do the document, or the
        empty list if none found. For example, given "1000", may
        return ["1000.a1", "1000.a2"]. May set self._read_only flag to
        True.
        """

        from os.path import isfile
        from os import access, W_OK

        try:
            # Do we have a valid suffix? If so, it is probably best to the file
            suff = document[document.rindex('.') + 1:]
            if suff == JOINED_ANN_FILE_SUFF:
                # It is a joined file, let's load it
                input_files = [document]
                # Do we lack write permissions?
                if not access(document, W_OK):
                    #TODO: Should raise an exception or warning
                    self._read_only = True
            elif suff in PARTIAL_ANN_FILE_SUFF:
                # It is only a partial annotation, we will most likely fail
                # but we will try opening it
                input_files = [document]
                self._read_only = True
            else:
                input_files = []
        except ValueError:
            # The document lacked a suffix
            input_files = []

        if not input_files:
            # Our first attempts at finding the input by checking suffixes
            # failed, so we try to attach know suffixes to the path.
            sugg_path = document + '.' + JOINED_ANN_FILE_SUFF
            if isfile(sugg_path):
                # We found a joined file by adding the joined suffix
                input_files = [sugg_path]
                # Do we lack write permissions?
                if not access(sugg_path, W_OK):
                    #TODO: Should raise an exception or warning
                    self._read_only = True
            else:
                # Our last shot, we go for as many partial files as possible
                input_files = [sugg_path for sugg_path in 
                        (document + '.' + suff
                            for suff in PARTIAL_ANN_FILE_SUFF)
                        if isfile(sugg_path)]
                self._read_only = True

        return input_files
            
    #TODO: DOC!
    def __init__(self, document, read_only=False):
        #TODO: DOC!
        #TODO: Incorparate file locking! Is the destructor called upon inter crash?
        from collections import defaultdict
        from os.path import basename, getmtime, getctime
        #from fileinput import FileInput, hook_encoded

        # we should remember this
        self._document = document

        self.failed_lines = []

        ### Here be dragons, these objects need constant updating and syncing
        # Annotation for each line of the file
        self._lines = []
        # Mapping between annotation objects and which line they occur on
        # Range: [0, inf.) unlike [1, inf.) which is common for files
        self._line_by_ann = {}
        # Maximum id number used for each id prefix, to speed up id generation
        #XXX: This is effectively broken by the introduction of id suffixes
        self._max_id_num_by_prefix = defaultdict(lambda : 1)
        # Annotation by id, not includid non-ided annotations 
        self._ann_by_id = {}
        ###

        ## We use some heuristics to find the appropriate annotation files
        self._read_only = read_only
        input_files = self._select_input_files(document)

        if not input_files:
            raise AnnotationFileNotFoundError(document)

        # We then try to open the files we got using the heuristics
        #self._file_input = FileInput(openhook=hook_encoded('utf-8'))
        self._input_files = input_files

        # Finally, parse the given annotation file
        self._parse_ann_file()

        #XXX: Hack to get the timestamps after parsing
        if (len(self._input_files) == 1 and
                self._input_files[0].endswith(JOINED_ANN_FILE_SUFF)):
            self.ann_mtime = getmtime(self._input_files[0])
            self.ann_ctime = getctime(self._input_files[0])
        else:
            # We don't have a single file, just set to epoch for now
            self.ann_mtime = 0
            self.ann_ctime = 0

    def get_events(self):
        return (a for a in self if isinstance(a, EventAnnotation))

    def get_equivs(self):
        return (a for a in self if isinstance(a, EquivAnnotation))

    def get_textbounds(self):
        return (a for a in self if isinstance(a, TextBoundAnnotation))

    def get_modifers(self):
        return (a for a in self if isinstance(a, ModifierAnnotation))

    def get_relations(self):
        return (a for a in self if isinstance(a, BinaryRelationAnnotation))

    def get_oneline_comments(self):
        #XXX: The status exception is for the document status protocol
        #       which is yet to be formalised
        return (a for a in self if isinstance(a, OnelineCommentAnnotation)
                and a.type != 'STATUS')

    def get_statuses(self):
        return (a for a in self if isinstance(a, OnelineCommentAnnotation)
                and a.type == 'STATUS')

    # TODO: getters for other categories of annotations
    #TODO: Remove read and use an internal and external version instead
    def add_annotation(self, ann, read=False):
        #TODO: DOC!
        #TODO: Check read only
        if not read and self._read_only:
            raise AnnotationsIsReadOnlyError

        # Equivs have to be merged with other equivs
        try:
            # Bail as soon as possible for non-equivs
            ann.entities # TODO: what is this?
            merge_cand = ann
            for eq_ann in self.get_equivs():
                try:
                    # Make sure that this Equiv duck quacks
                    eq_ann.entities
                except AttributeError, e:
                    assert False, 'got a non-entity from an entity call'

                # Do we have an entitiy in common with this equiv?
                for ent in merge_cand.entities:
                    if ent in eq_ann.entities:
                        for m_ent in merge_cand.entities:
                            if m_ent not in eq_ann.entities: 
                                eq_ann.entities.append(m_ent)
                        # Don't try to delete ann since it never was added
                        if merge_cand != ann:
                            try:
                                self.del_annotation(merge_cand)
                            except DependingAnnotationDeleteError:
                                assert False, ('Equivs lack ids and should '
                                        'never have dependent annotations')
                        merge_cand = eq_ann
                        # We already merged it all, break to the next ann
                        break

            if merge_cand != ann:
                # The proposed annotation was simply merged, no need to add it
                # Update the modification time
                from time import time
                self.ann_mtime = time()
                return

        except AttributeError:
            #XXX: This can catch a ton more than we want to! Ugly!
            # It was not an Equiv, skip along
            pass

        # Register the object id
        try:
            self._ann_by_id[ann.id] = ann
            pre, num = annotation_id_prefix(ann.id), annotation_id_number(ann.id)
            self._max_id_num_by_prefix[pre] = max(num, self._max_id_num_by_prefix[pre])
        except AttributeError:
            # The annotation simply lacked an id which is fine
            pass

        # Add the annotation as the last line
        self._lines.append(ann)
        self._line_by_ann[ann] = len(self) - 1
        # Update the modification time
        from time import time
        self.ann_mtime = time()

    def del_annotation(self, ann, tracker=None):
        #TODO: Check read only
        #TODO: Flag to allow recursion
        #TODO: Sampo wants to allow delet of direct deps but not indirect, one step
        #TODO: needed to pass tracker to track recursive mods, but use is too
        #      invasive (direct modification of ModificationTracker.deleted)
        #TODO: DOC!
        if self._read_only:
            raise AnnotationsIsReadOnlyError

        try:
            ann.id
        except AttributeError:
            # If it doesn't have an id, nothing can depend on it
            if tracker is not None:
                tracker.deletion(ann)
            self._atomic_del_annotation(ann)
            # Update the modification time
            from time import time
            self.ann_mtime = time()
            return

        # collect annotations dependending on ann
        ann_deps = []

        for other_ann in self:
            soft_deps, hard_deps = other_ann.get_deps()
            if unicode(ann.id) in soft_deps | hard_deps:
                ann_deps.append(other_ann)
              
        # If all depending are ModifierAnnotations or EquivAnnotations,
        # delete all modifiers recursively (without confirmation) and remove
        # the annotation id from the equivs (and remove the equiv if there is
        # only one id left in the equiv)
        # Note: this assumes ModifierAnnotations cannot have
        # other dependencies depending on them, nor can EquivAnnotations
        if all((False for d in ann_deps if (
            not isinstance(d, ModifierAnnotation)
            and not isinstance(d, EquivAnnotation)
            and not isinstance(d, OnelineCommentAnnotation)
            ))):

            for d in ann_deps:
                if isinstance(d, ModifierAnnotation):
                    if tracker is not None:
                        tracker.deletion(d)
                    self._atomic_del_annotation(d)
                elif isinstance(d, EquivAnnotation):
                    if len(d.entities) <= 2:
                        # An equiv has to have more than one member
                        self._atomic_del_annotation(d)
                        if tracker is not None:
                            tracker.deletion(d)
                    else:
                        if tracker is not None:
                            before = unicode(d)
                        d.entities.remove(unicode(ann.id))
                        if tracker is not None:
                            tracker.change(before, d)
                elif isinstance(d, OnelineCommentAnnotation):
                    #TODO: Can't anything refer to comments?
                    self._atomic_del_annotation(d)
                    if tracker is not None:
                        tracker.deletion(d)
            ann_deps = []
            
        if ann_deps:
            raise DependingAnnotationDeleteError(ann, ann_deps)

        if tracker is not None:
            tracker.deletion(ann)
        self._atomic_del_annotation(ann)

    def _atomic_del_annotation(self, ann):
        #TODO: DOC
        # Erase the ann by id shorthand
        try:
            del self._ann_by_id[ann.id]
        except AttributeError:
            # So, we did not have id to erase in the first place
            pass

        ann_line = self._line_by_ann[ann]
        # Erase the main annotation
        del self._lines[ann_line]
        # Erase the ann by line shorthand
        del self._line_by_ann[ann]
        # Update the line shorthand of every annotation after this one
        # to reflect the new self._lines
        for l_num in xrange(ann_line, len(self)):
            self._line_by_ann[self[l_num]] = l_num
        # Update the modification time
        from time import time
        self.ann_mtime = time()
    
    def get_ann_by_id(self, id):
        #TODO: DOC
        try:
            return self._ann_by_id[id]
        except KeyError:
            raise AnnotationNotFoundError(id)

    def get_new_id(self, prefix, suffix=None):
        '''
        Return a new valid unique id for this annotation file for the given
        prefix. No ids are re-used for traceability over time for annotations,
        but this only holds for the lifetime of the annotation object. If the
        annotation file is parsed once again into an annotation object the
        next assigned id will be the maximum seen for a given prefix plus one
        which could have been deleted during a previous annotation session.

        Warning: get_new_id('T') == get_new_id('T')
        Just calling this method does not reserve the id, you need to
        add the annotation with the returned id to the annotation object in
        order to reserve it.

        Argument(s):
        id_pre - an annotation prefix on the format [A-Za-z]+

        Returns:
        An id that is guaranteed to be unique for the lifetime of the
        annotation.
        '''
        #XXX: We have changed this one radically!
        #XXX: Stupid and linear
        if suffix is None:
            suffix = ''
        #XXX: Arbitary constant!
        for suggestion in (prefix + unicode(i) + suffix for i in xrange(1, 2**15)):
            # This is getting more complicated by the minute, two checks since
            # the developers no longer know when it is an id or string.
            if suggestion not in self._ann_by_id:
                return suggestion

    def _parse_event_annotation(self, id, data, data_tail):
        #XXX: A bit nasty, we require a single space
        try:
            type_delim = data.index(' ')
            type_trigger, type_trigger_tail = (data[:type_delim], data[type_delim:])
        except ValueError:
            type_trigger = data.rstrip('\r\n')
            type_trigger_tail = None

        try:
            type, trigger = type_trigger.split(':')
        except ValueError:
            # TODO: consider accepting events without triggers, e.g.
            # BioNLP ST 2011 Bacteria task
            raise IdedAnnotationLineSyntaxError(id, self.ann_line, self.ann_line_num+1)

        if type_trigger_tail is not None:
            args = [tuple(arg.split(':')) for arg in type_trigger_tail.split()]
        else:
            args = []

        return EventAnnotation(trigger, args, id, type, data_tail)

    def _parse_relation_annotation(self, id, data, data_tail):
        try:
            type_delim = data.index(' ')
            type, type_tail = (data[:type_delim], data[type_delim:])
        except ValueError:
            # cannot have a relation with just a type (contra event)
            raise IdedAnnotationLineSyntaxError(id, self.ann_line, self.ann_line_num+1)
            
        try:
            args = [tuple(arg.split(':')) for arg in type_tail.split()]
        except ValueError:
            raise IdedAnnotationLineSyntaxError(id, self.ann_line, self.ann_line_num+1)

        if len(args) != 2:
            display_message("Error parsing relation: must have exactly two arguments", "error")
            raise IdedAnnotationLineSyntaxError(id, self.ann_line, self.ann_line_num+1)

        args.sort()
        if args[0][0] != "Arg1" or args[1][0] != "Arg2":
            display_message("Error parsing relation: arguments must be \"Arg1\" and \"Arg2\"", "error")
            raise IdedAnnotationLineSyntaxError(id, self.ann_line, self.ann_line_num+1)

        return BinaryRelationAnnotation(id, type, args[0][1], args[1][1], data_tail)

    def _parse_equiv_annotation(self, id, data, data_tail):
        # TODO: this will split on any space, which is likely not correct
        type, type_tail = data.split(None, 1)
        if type != 'Equiv':
            raise IdedAnnotationLineSyntaxError(id, self.ann_line, self.ann_line_num+1)
        equivs = type_tail.split(None)
        return EquivAnnotation(type, equivs, data_tail)

    def _parse_modifier_annotation(self, id, data, data_tail):
        type, target = data.split()
        return ModifierAnnotation(target, id, type, data_tail)

    def _split_textbound_data(self, id, data):
        try:
            type, start_str, end_str = data.split(None, 2)
            # ignore trailing whitespace
            end_str = end_str.rstrip()
            # Abort if we have trailing values, i.e. space-separated tail in end_str
            if any((c.isspace() for c in end_str)):
                #display_message("Error parsing textbound '%s\t%s'. (Using space instead of tab?)" % (id, data), "error")
                raise IdedAnnotationLineSyntaxError(id, self.ann_line, self.ann_line_num+1)
            start, end = (int(start_str), int(end_str))
        except:
            raise IdedAnnotationLineSyntaxError(id, self.ann_line, self.ann_line_num+1)
            
        return type, start, end

    def _parse_textbound_annotation(self, id, data, data_tail):
        type, start, end = self._split_textbound_data(id, data)
        return TextBoundAnnotation(start, end, id, type, data_tail)

    def _parse_comment_line(self, id, data, data_tail):
        try:
            type, target = data.split()
        except ValueError:
            raise IdedAnnotationLineSyntaxError(id, self.ann_line, self.ann_line_num+1)
        return OnelineCommentAnnotation(target, id, type, data_tail)
    
    def _parse_ann_file(self):
        from itertools import takewhile

        self.ann_line_num = -1
        for input_file_path in self._input_files:
            with open_textfile(input_file_path) as input_file:
                #for self.ann_line_num, self.ann_line in enumerate(self._file_input):
                for self.ann_line in input_file:
                    self.ann_line_num += 1
                    try:
                        # ID processing
                        try:
                            id, id_tail = self.ann_line.split('\t', 1)
                        except ValueError:
                            raise AnnotationLineSyntaxError(self.ann_line, self.ann_line_num+1)

                        pre = annotation_id_prefix(id)

                        if id in self._ann_by_id and pre != '*':
                            raise DuplicateAnnotationIdError(id)

                        # Cases for lines
                        try:
                            data_delim = id_tail.index('\t')
                            data, data_tail = (id_tail[:data_delim],
                                    id_tail[data_delim:])
                        except ValueError:
                            data = id_tail
                            # No tail at all, although it should have a \t
                            data_tail = ''

                        new_ann = None

                        if pre == '*':
                            new_ann = self._parse_equiv_annotation(id, data, data_tail)
                        elif pre == 'E':
                            new_ann = self._parse_event_annotation(id, data, data_tail)
                        elif pre == 'M':
                            new_ann = self._parse_modifier_annotation(id, data, data_tail)
                        elif pre == 'T':
                            new_ann = self._parse_textbound_annotation(id, data, data_tail)
                        elif pre == '#':
                            new_ann = self._parse_comment_line(id, data, data_tail)
                        elif pre == 'R':
                            new_ann = self._parse_relation_annotation(id, data, data_tail)
                        else:
                            raise IdedAnnotationLineSyntaxError(id, self.ann_line, self.ann_line_num+1)

                        assert new_ann is not None, "INTERNAL ERROR"
                        self.add_annotation(new_ann, read=True)

                    except IdedAnnotationLineSyntaxError, e:
                        # Could parse an ID but not the whole line; add UnparsedIdedAnnotation
                        self.add_annotation(UnparsedIdedAnnotation(e.id, e.line), read=True)
                        self.failed_lines.append(e.line_num - 1)

                    except AnnotationLineSyntaxError, e:
                        # We could not parse even an ID on the line, just add it as an unknown annotation
                        self.add_annotation(UnknownAnnotation(e.line), read=True)
                        # NOTE: For access we start at line 0, not 1 as in here
                        self.failed_lines.append(e.line_num - 1)

    def __str__(self):
        s = u'\n'.join(unicode(ann).rstrip(u'\r\n') for ann in self)
        if not s:
            return u''
        else:
            return s if s[-1] == u'\n' else s + u'\n'

    def __it__(self):
        for ann in self._lines:
            yield ann

    def __getitem__(self, val):
        try:
            # First, try to use it as a slice object
            return self._lines[val.start, val.stop, val.step]
        except AttributeError:
            # It appears not to be a slice object, try it as an index
            return self._lines[val]

    def __len__(self):
        return len(self._lines)

    def __enter__(self):
        # No need to do any handling here, the constructor handles that
        return self
    
    def __exit__(self, type, value, traceback):
        #self._file_input.close()
        if not self._read_only:
            assert len(self._input_files) == 1, 'more than one valid outfile'

            # We are hitting the disk a lot more than we should here, what we
            # should have is a modification flag in the object but we can't
            # due to how we change the annotations.
            
            out_str = unicode(self)
            with open_textfile(self._input_files[0], 'r') as old_ann_file:
                old_str = old_ann_file.read()

            # Was it changed?
            if out_str == old_str:
                # Then just return
                return
            
            # Protect the write so we don't corrupt the file
            with file_lock(path_join(WORK_DIR,
                    basename(self._input_files[0].replace('/', '_')))
                    ) as lock_file:
                #from tempfile import NamedTemporaryFile
                from tempfile import mkstemp
                # TODO: XXX: Is copyfile really atomic?
                from shutil import copyfile
                # XXX: NamedTemporaryFile only supports encoding for Python 3
                #       so we hack around it.
                #with NamedTemporaryFile('w', suffix='.ann') as tmp_file:
                # Grab the filename, but discard the handle
                _, tmp_fname = mkstemp(suffix='.ann')
                try:
                    with open_textfile(tmp_fname, 'w') as tmp_file:
                        #XXX: Temporary hack to make sure we don't write corrupted
                        #       files, but the client will already have the version
                        #       at this stage leading to potential problems upon
                        #       the next change to the file.
                        tmp_file.write(out_str)
                        tmp_file.flush()

                        try:
                            with Annotations(tmp_file.name) as ann:
                                # Move the temporary file onto the old file
                                copyfile(tmp_file.name, self._input_files[0])
                                # As a matter of convention we adjust the modified
                                # time of the data dir when we write to it. This
                                # helps us to make back-ups
                                now = time()
                                #XXX: Disabled for now!
                                #utime(DATA_DIR, (now, now))
                        except Exception, e:
                            from message import display_message
                            display_message('ERROR writing changes: generated annotations cannot be read back in!<br/>(This is almost certainly a system error, please contact the developers.)<br/>%s' % e, 'error', -1)
                            raise
                finally:
                    from os import remove
                    remove(tmp_fname)
            return

    def __in__(self, other):
        #XXX: You should do this one!
        pass

class TextAnnotations(Annotations):
    """
    Text-bound annotation storage. Extends Annotations in assuming
    access to text text to which the annotations apply and verifying
    the correctness of text-bound annotations against the text.
    """
    def __init__(self, document, read_only=False):
        # start by reading in the document text for textbounds.
        document_text = self._read_document_text(document)
        if not document_text:
            raise AnnotationTextFileNotFoundError(document)
        self._document_text = document_text

        Annotations.__init__(self, document, read_only) 

    def _parse_textbound_annotation(self, id, data, data_tail):
        type, start, end = self._split_textbound_data(id, data)

        # Verify annotation extent
        if start > end:
            display_message("Text-bound annotation start &gt; end", "error")
            raise IdedAnnotationLineSyntaxError(id, self.ann_line, self.ann_line_num+1)
        if start < 0:
            display_message("Text-bound annotation start &lt; 0.", "error")
            raise IdedAnnotationLineSyntaxError(id, self.ann_line, self.ann_line_num+1)
        if end > len(self._document_text):
            display_message("Text-bound annotation offset exceeds text length.", "error")
            raise IdedAnnotationLineSyntaxError(id, self.ann_line, self.ann_line_num+1)

        # Require tail to be either empty or to begin with the text
        # corresponding to the start:end span. If the tail is empty,
        # force a fill with the corresponding text.
        if data_tail.strip() == '':
            display_message(u"Text-bound annotation missing text (expected format 'ID\\tTYPE START END\\tTEXT'). Filling from reference text. NOTE: This changes annotations on disk unless read-only.", "warning", duration=-1)
            text = self._document_text[start:end]
        elif data_tail[0] != '\t':
            display_message("Text-bound annotation missing tab before text (expected format 'ID\\tTYPE START END\\tTEXT').", "error")
            raise IdedAnnotationLineSyntaxError(id, self.ann_line, self.ann_line_num+1)
        elif end-start > len(data_tail)-1: # -1 for tab
            display_message("Text-bound annotation text '%s' shorter than marked span %d:%d" % (data_tail[1:], start, end), "error")
            raise IdedAnnotationLineSyntaxError(id, self.ann_line, self.ann_line_num+1)
        else:
            text = data_tail[1:end-start+1] # shift 1 for tab
            data_tail = data_tail[end-start+1:]
            if text != self._document_text[start:end]:
                #log_info(text.__class__.__name__)
                display_message((u"Text-bound annotation text '%s' does not "
                    u"match marked span (%d:%d) text '%s' in document") % (
                        text,
                        start,
                        end,
                        self._document_text[start:end], 
                        ),
                    "error")
                raise IdedAnnotationLineSyntaxError(id, self.ann_line, self.ann_line_num+1)
            if data_tail != '' and not data_tail[0].isspace():
                display_message(u"Text-bound annotation text '%s' not separated from rest of line ('%s') by space!" % (text, data_tail), "error")
                raise IdedAnnotationLineSyntaxError(id, self.ann_line, self.ann_line_num+1)

        return TextBoundAnnotationWithText(start, end, id, type, text, data_tail)

    def _read_document_text(self, document):
        # TODO: this is too naive; document may be e.g. "PMID.a1",
        # in which case the reasonable text file name guess is
        # "PMID.txt", not "PMID.a1.txt"
        textfn = document+"."+TEXT_FILE_SUFFIX
        try:
            with open_textfile(textfn, 'r') as f:
                text = f.read()
                return text
        except:
            display_message("Error reading document text from %s" % textfn, "error")
        return None

class Annotation(object):
    """
    Base class for all annotations.
    """
    def __init__(self, tail):
        self.tail = tail

    def __str__(self):
        raise NotImplementedError

    def __repr__(self):
        return u'%s("%s")' % (unicode(self.__class__), unicode(self))
    
    def get_deps(self):
        return (set(), set())

class UnknownAnnotation(Annotation):
    """
    Represents a line of annotation that could not be parsed.
    These are not discarded, but rather passed through unmodified.
    """
    def __init__(self, line):
        Annotation.__init__(self, line)

    def __str__(self):
        return self.tail

class UnparsedIdedAnnotation(Annotation):
    """
    Represents an annotation for which an ID could be read but the
    rest of the line could not be parsed. This is separate from
    UnknownAnnotation to allow IDs for unparsed annotations to be
    "reserved".
    """
    # duck-type instead of inheriting from IdedAnnotation as
    # that inherits from TypedAnnotation and we have no type
    def __init__(self, id, line):
        # (this actually is the whole line, not just the id tail,
        # although Annotation will assign it to self.tail)
        Annotation.__init__(self, line)
        self.id = id

    def __str__(self):
        return unicode(self.tail)

class TypedAnnotation(Annotation):
    """
    Base class for all annotations with a type.
    """
    def __init__(self, type, tail):
        Annotation.__init__(self, tail)
        self.type = type

    def __str__(self):
        raise NotImplementedError

class IdedAnnotation(TypedAnnotation):
    """
    Base class for all annotations with an ID.
    """
    def __init__(self, id, type, tail):
        TypedAnnotation.__init__(self, type, tail)
        self.id = id

    def reference_id(self):
        # Return list that uniquely identifies this annotation within the document
        return [self.id]

    def __str__(self):
        raise NotImplementedError

class EventAnnotation(IdedAnnotation):
    """
    Represents an event annotation. Events are typed annotations that
    are associated with a specific text expression stating the event
    (TRIGGER, identifying a TextBoundAnnotation) and have an arbitrary
    number of arguments, each of which is represented as a ROLE:PARTID
    pair, where ROLE is a string identifying the role (e.g. "Theme",
    "Cause") and PARTID the ID of another annotation participating in
    the event.

    Represented in standoff as

    ID\tTYPE:TRIGGER [ROLE1:PART1 ROLE2:PART2 ...]
    """
    def __init__(self, trigger, args, id, type, tail):
        IdedAnnotation.__init__(self, id, type, tail)
        self.trigger = trigger
        self.args = args

    def __str__(self):
        return u'%s\t%s:%s %s%s' % (
                self.id,
                self.type,
                self.trigger,
                ' '.join([':'.join(map(str, arg_tup))
                    for arg_tup in self.args]),
                self.tail
                )

    def get_deps(self):
        soft_deps, hard_deps = IdedAnnotation.get_deps(self)
        hard_deps.add(self.trigger)
        arg_ids = [arg_tup[1] for arg_tup in self.args]
        # TODO: verify this logic, it's not entirely clear it's right
        if len(arg_ids) > 1:
            for arg in arg_ids:
                soft_deps.add(arg)
        else:
            for arg in arg_ids:
                hard_deps.add(arg)
        return (soft_deps, hard_deps)


class EquivAnnotation(TypedAnnotation):
    """
    Represents an equivalence group annotation. Equivs define a set of
    other annotations (normally TextBoundAnnotation) to be equivalent.

    Represented in standoff as
    
    *\tEquiv ID1 ID2 [...]

    Where "*" is the literal asterisk character.
    """
    def __init__(self, type, entities, tail):
        TypedAnnotation.__init__(self, type, tail)
        self.entities = entities

    def __in__(self, other):
        return other in self.entities

    def __str__(self):
        return u'*\t%s %s%s' % (
                self.type,
                ' '.join([unicode(e) for e in self.entities]),
                self.tail
                )

    def get_deps(self):
        soft_deps, hard_deps = TypedAnnotation.get_deps(self)
        if len(self.entities) > 2:
            for ent in self.entities:
                soft_deps.add(ent)
        else:
            for ent in self.entities:
                hard_deps.add(ent)
        return (soft_deps, hard_deps)

    def reference_id(self):
        if self.entities:
            return ['equiv', self.type, self.entities[0]]
        else:
            return ['equiv', self.type, self.entities]

class ModifierAnnotation(IdedAnnotation):
    def __init__(self, target, id, type, tail):
        IdedAnnotation.__init__(self, id, type, tail)
        self.target = target
        
    def __str__(self):
        return u'%s\t%s %s%s' % (
                self.id,
                self.type,
                self.target,
                self.tail
                )

    def get_deps(self):
        soft_deps, hard_deps = IdedAnnotation.get_deps(self)
        hard_deps.add(self.target)
        return (soft_deps, hard_deps)

    def reference_id(self):
        # TODO: can't currently ID modifier in isolation; return
        # reference to modified instead
        return [self.target]

class OnelineCommentAnnotation(IdedAnnotation):
    def __init__(self, target, id, type, tail):
        IdedAnnotation.__init__(self, id, type, tail)
        self.target = target
        
    def __str__(self):
        return u'%s\t%s %s%s' % (
                self.id,
                self.type,
                self.target,
                self.tail
                )

    def get_deps(self):
        soft_deps, hard_deps = IdedAnnotation.get_deps(self)
        hard_deps.add(self.target)
        return (soft_deps, hard_deps)


class TextBoundAnnotation(IdedAnnotation):
    """
    Represents a text-bound annotation. Text-bound annotations
    identify a specific span of text and assign it a type.  This base
    class does not assume ability to access text; use
    TextBoundAnnotationWithText for that.

    Represented in standoff as
    
    ID\tTYPE START END

    Where START and END are positive integer offsets identifying the
    span of the annotation in text.
    """

    def __init__(self, start, end, id, type, tail):
        # Note: if present, the text goes into tail
        IdedAnnotation.__init__(self, id, type, tail)
        self.start = start
        self.end = end

    def get_text(self):
        # If you're seeing this exception, you probably need a
        # TextBoundAnnotationWithText. The underlying issue may be
        # that you're creating an Annotations object instead of
        # TextAnnotations.
        raise NotImplementedError

    def __str__(self):
        return u'%s\t%s %s %s%s' % (
                self.id,
                self.type,
                self.start,
                self.end,
                self.tail
                )

class TextBoundAnnotationWithText(TextBoundAnnotation):
    """
    Represents a text-bound annotation. Text-bound annotations
    identify a specific span of text and assign it a type.  This class
    assume that the referenced text is included in the annotation.

    Represented in standoff as

    ID\tTYPE START END\tTEXT

    Where START and END are positive integer offsets identifying the
    span of the annotation in text and TEXT is the corresponding text.
    """
    def __init__(self, start, end, id, type, text, text_tail=""):
        IdedAnnotation.__init__(self, id, type, '\t'+text+text_tail)
        self.start = start
        self.end = end
        self.text = text
        self.text_tail = text_tail

    def get_text(self):
        return self.text

    def __str__(self):
        #log_info('TextBoundAnnotationWithText: __str__: "%s"' % self.text)
        return u'%s\t%s %s %s\t%s%s' % (
                self.id,
                self.type,
                self.start,
                self.end,
                self.text,
                self.text_tail
                )

class BinaryRelationAnnotation(IdedAnnotation):
    """
    Represents a typed binary relation annotation. Relations are
    assumed not to be symmetric (i.e are "directed"); for equivalence
    relations, EquivAnnotation is likely to be more appropriate.
    Unlike events, relations are not associated with text expressions
    (triggers) stating them.

    Represented in standoff as

    ID\tTYPE Arg1:ID1 Arg2:ID2

    Where "Arg1" and "Arg2" are literal strings.
    """
    def __init__(self, id, type, arg1, arg2, tail):
        IdedAnnotation.__init__(self, id, type, tail)
        self.arg1 = arg1
        self.arg2 = arg2

    def __str__(self):
        return u'%s\t%s Arg1:%s Arg2:%s%s' % (
            self.id,
            self.type,
            self.arg1,
            self.arg2,
            self.tail
            )
    
    def get_deps(self):
        soft_deps, hard_deps = IdedAnnotation.get_deps(self)
        hard_deps.add(self.arg1)
        hard_deps.add(self.arg2)
        return soft_deps, hard_deps

if __name__ == '__main__':
    #TODO: Unit-testing
    pass
