/* ====================================================================
 * The Apache Software License, Version 1.1
 *
 * Copyright (c) 2000-2002 The Apache Software Foundation.  All rights
 * reserved.
 *
 * Redistribution and use in source and binary forms, with or without
 * modification, are permitted provided that the following conditions
 * are met:
 *
 * 1. Redistributions of source code must retain the above copyright
 *    notice, this list of conditions and the following disclaimer.
 *
 * 2. Redistributions in binary form must reproduce the above copyright
 *    notice, this list of conditions and the following disclaimer in
 *    the documentation and/or other materials provided with the
 *    distribution.
 *
 * 3. The end-user documentation included with the redistribution,
 *    if any, must include the following acknowledgment:
 *       "This product includes software developed by the
 *        Apache Software Foundation (http://www.apache.org/)."
 *    Alternately, this acknowledgment may appear in the software itself,
 *    if and wherever such third-party acknowledgments normally appear.
 *
 * 4. The names "Apache" and "Apache Software Foundation" must
 *    not be used to endorse or promote products derived from this
 *    software without prior written permission. For written
 *    permission, please contact apache@apache.org.
 *
 * 5. Products derived from this software may not be called "Apache",
 *    "mod_python", or "modpython", nor may these terms appear in their
 *    name, without prior written permission of the Apache Software
 *    Foundation.
 *
 * THIS SOFTWARE IS PROVIDED ``AS IS'' AND ANY EXPRESSED OR IMPLIED
 * WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE IMPLIED WARRANTIES
 * OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE ARE
 * DISCLAIMED.  IN NO EVENT SHALL THE APACHE SOFTWARE FOUNDATION OR
 * ITS CONTRIBUTORS BE LIABLE FOR ANY DIRECT, INDIRECT, INCIDENTAL,
 * SPECIAL, EXEMPLARY, OR CONSEQUENTIAL DAMAGES (INCLUDING, BUT NOT
 * LIMITED TO, PROCUREMENT OF SUBSTITUTE GOODS OR SERVICES; LOSS OF
 * USE, DATA, OR PROFITS; OR BUSINESS INTERRUPTION) HOWEVER CAUSED AND
 * ON ANY THEORY OF LIABILITY, WHETHER IN CONTRACT, STRICT LIABILITY,
 * OR TORT (INCLUDING NEGLIGENCE OR OTHERWISE) ARISING IN ANY WAY OUT
 * OF THE USE OF THIS SOFTWARE, EVEN IF ADVISED OF THE POSSIBILITY OF
 * SUCH DAMAGE.
 * ====================================================================
 *
 * This software consists of voluntary contributions made by many
 * individuals on behalf of the Apache Software Foundation.  For more
 * information on the Apache Software Foundation, please see
 * <http://www.apache.org/>.
 *
 * $Id$
 *
 * See accompanying documentation and source code comments 
 * for details.
 *
 */

#include "psp_string.h"

#define psp_string_alloc(__pspstring, __length) \
        if ((__length) > (__pspstring)->allocated) { \
                (__pspstring)->blob = realloc((__pspstring)->blob, (__length) + PSP_STRING_BLOCK); \
                (__pspstring)->allocated = (__length) + PSP_STRING_BLOCK; \
        }

void 
psp_string_0(psp_string *s)
{
        if (!s->length) {
                return;
        }

        s->blob[s->length] = '\0';
}

void
psp_string_appendl(psp_string *s, char *text, size_t length)
{
        int newlen = s->length + length;

        if (text == NULL) {
                return;
        }
        
        psp_string_alloc(s, newlen);
        memcpy(s->blob + s->length, text, length);
        s->length = newlen;
}

void
psp_string_append(psp_string *s, char *text)
{
        if (text == NULL) {
                return;
        }
        psp_string_appendl(s, text, strlen(text));
}

void 
psp_string_appendc(psp_string *s, char c)
{
        int newlen = s->length + 1;
        
        psp_string_alloc(s, newlen);
        s->blob[s->length] = c;
        s->length = newlen;
}

void 
psp_string_clear(psp_string *s)
{
        memset(s->blob, 0, s->length);
        s->length = 0;
}

void
psp_string_free(psp_string *s)
{
        free(s->blob);
        s->blob = NULL;
        s->length = 0;
        s->allocated = 0;
}               
