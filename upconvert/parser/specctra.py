#!/usr/bin/env python2
""" The Specctra DSN Format Parser """

# upconvert - A universal hardware design file format converter using
# Format: upverter.com/resources/open-json-format/
# Development: github.com/upverter/schematic-file-converter
#
# Copyright 2011 Upverter, Inc.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

#
# TODO: !!! The following is incorrect, need to fix !!!
# Pads can have different shapes in DSN format (padstack), JSON format
# supports only lines. So make all pad shapes part of the component
# both in DSN->UPV and UPV->DSN directions.
#

from upconvert.core.design import Design
from upconvert.core.components import Component, Footprint, FBody, Pad
from upconvert.core.component_instance import ComponentInstance, FootprintAttribute
from upconvert.core.net import Net, NetPoint, ConnectedComponent
from upconvert.core.trace import Trace
from upconvert.core.shape import Circle, Line, Rectangle, Polygon, Point, Arc

from string import whitespace
from sys import maxint

from upconvert.parser import specctraobj 
import math

class Specctra(object):
    """ The Specctra DSN Format Parser """
    def __init__(self):
        self.design = None
        self.resolution = None
        self.nets = {}
        self.net_points = {}
        self._id = 10
        self.min_x = maxint
        self.max_x = -(maxint - 1)
        self.min_y = maxint
        self.max_y = -(maxint - 1)

    @staticmethod
    def auto_detect(filename):
        """ Return our confidence that the given file is an specctra schematic """
        with open(filename, 'r') as f:
            data = f.read(4096)
        confidence = 0
        if '(pcb ' in data or '(PCB ' in data:
            confidence += 0.75
        return confidence

    def parse(self, filename):
        """ Parse a specctra file into a design """

        self.design = Design()

        with open(filename) as f:
            data = f.read()

        tree = DsnParser().parse(data)

        struct = self.walk(tree)
        self.resolution = struct.resolution
        self._convert(struct)

        return self.design

    def _convert(self, struct):
        for bound in struct.structure.boundary:
            if bound.rectangle.layer_id == 'pcb':
                vertex1, vertex2 = bound.rectangle.vertex1, bound.rectangle.vertex2
                self.min_x = self.to_pixels(min(vertex1[0], vertex2[0]))
                self.max_x = self.to_pixels(max(vertex1[0], vertex2[0]))
                self.min_y = self.to_pixels(min(vertex1[1], vertex2[1]))
                self.max_y = self.to_pixels(max(vertex1[1], vertex2[1]))
                break

        self._convert_library(struct)
        self._convert_components(struct)
        self._convert_nets(struct)

    def _convert_library(self, struct):
        """ Convert library """
        for image in struct.library.image:
            component = Component(image.image_id)
            self.design.add_component(image.image_id, component)
            fpt = Footprint()
            body = FBody()
            component.add_footprint(fpt)
            fpt.add_body(body)
            for pad in image.pin:
                body.add_pad(Pad(pad.pad_id, self.to_pixels(pad.vertex), self.to_pixels(pad.vertex)))
                for padstack in struct.library.padstack:
                    if padstack.padstack_id == pad.padstack_id:
                        shapes = [shape.shape for shape in padstack.shape]
                        for shape in self._convert_shapes(shapes, self.to_pixels(pad.vertex)):
                            body.add_shape(shape)
                        break

            for outline in image.outline:
                for shape in self._convert_shapes([outline.shape]):
                    body.add_shape(shape)

    def _convert_components(self, struct):
        """ Convert component """
        for component in struct.placement.component:
            library_id = component.image_id
            for place in component.place:
                # Outside PCB boundary
                if not place.vertex:
                    continue

                mirror = {90:270, 270:90}
                if place.side == 'back':
                    rotation = place.rotation
                else:
                    rotation = mirror.get(int(place.rotation), place.rotation)
                inst = ComponentInstance(place.component_id, component, library_id, 0)
                v = self.to_pixels(place.vertex)
                symbattr = FootprintAttribute(v[0], v[1], to_piradians(rotation), False)
                inst.add_symbol_attribute(symbattr) 
                self.design.add_component_instance(inst)

    def _get_point(self, net_id, point_id, x, y):
        if net_id not in self.nets:
            n = Net(net_id)
            self.design.add_net(n)
            self.nets[n.net_id] = n
        else:
            n = self.nets[net_id]

        key = (x, y)
        if key not in self.net_points:
            if not point_id:
                point_id = str(self._id)
                self._id += 1
            np = NetPoint(net_id + '-' + point_id, x, y)
            n.add_point(np)
            self.net_points[key] = np
        else:
            np = self.net_points[key]

        return np
 
    def _convert_wires(self, struct):
        if struct.wiring:
            for wire in struct.wiring.wire:
                lines = self._convert_shapes([wire.shape], absolute=True)
                for line in lines:
                    try:
                        np1 = self._get_point(wire.net.net_id, None, line.p1.x, line.p1.y)
                        np2 = self._get_point(wire.net.net_id, None, line.p2.x, line.p2.y)

                        np1.add_connected_point(np2.point_id)
                        np2.add_connected_point(np1.point_id)
                    except: 
                        pass

    def _convert_nets(self, struct):
        """ Convert nets """
        # FIXME polyline_path is not documented and no success with reverse engineering yet
        self._convert_wires(struct)

        if struct.network:
            for net in struct.network.net:
                if net.pins is None:
                    continue

                prev_point = None
                for pin_ref in net.pins.pin_reference:
                    # pin_ref like A1-"-" is valid (parsed to A1--)
                    component_id, pin_id = pin_ref[:pin_ref.index('-')], pin_ref[pin_ref.index('-') + 1:]  
                    point = self.get_component_pin(component_id, pin_id)
                    if point is None:
                        print 'Could not find net %s pin ref %s' % (net.net_id, pin_ref)
                        continue
                    cc = ConnectedComponent(component_id, pin_id)
                    np = self._get_point(net.net_id, pin_ref, point[0], point[1])
                    np.add_connected_component(cc)

                    if prev_point is not None:
                        # XXX if point is already connected assume wiring had routed network, don't do it here
                        if len(prev_point.connected_points) == 0:
                            prev_point.add_connected_point(np.point_id)
                        if len(np.connected_points) == 0:
                            np.add_connected_point(prev_point.point_id)
                    prev_point = np

    def get_component_pin(self, component_id, pin_id):
        for component_instance in self.design.component_instances:
            symbattr = component_instance.symbol_attributes[0]
            if component_instance.instance_id == component_id: 
                component = self.design.components.components[component_instance.library_id]
                for pin in component.symbols[0].bodies[0].pins:
                    if pin.pin_number == pin_id:
                        x, y = rotate((pin.p1.x, pin.p1.y), symbattr.rotation)
                        return (symbattr.x + x, symbattr.y + y)

    def _make_line(self, p1, p2, aperture):
        x1, y1 = float(p1[0]), float(p1[1])
        x2, y2 = float(p2[0]), float(p2[1])
        aperture = float(aperture)

        dx = x2 - x1
        dy = y2 - y1
        length = math.sqrt(dx * dx + dy * dy)

        if length == 0.0:
            return []
        result = []

        line1 = Line(
                (x1 - aperture * (y2 - y1) / length,
                y1 - aperture * (x1 - x2) / length),
                (x2 - aperture * (y2 - y1) / length,
                y2 - aperture * (x1 - x2) / length)
            )
        line2 = Line(
                (x1 + aperture * (y2 - y1) / length,
                y1 + aperture * (x1 - x2) / length),
                (x2 + aperture * (y2 - y1) / length,
                y2 + aperture * (x1 - x2) / length)
            )
        result.append(line1)
        result.append(line2)

        def make_arc(p1, p2, p0):
            start_angle = math.atan2(p1.y - p0.y, p1.x - p0.x) / math.pi
            end_angle = math.atan2(p2.y - p0.y, p2.x - p0.x) / math.pi
            return Arc(p0.x, p0.y, start_angle, end_angle, aperture)

        result.append(make_arc(line1.p1, line2.p1, Point(p1)))
        result.append(make_arc(line2.p2, line1.p2, Point(p2)))
        return result
   
    def _convert_path(self, aperture, points):
        """ Convert path """
        result = []
        prev = points[0]
        for point in points[1:]:
            line = self._make_line(prev, point, float(aperture) / 2.0)
            result.extend(line)
            prev = point
        return result

    def _convert_shapes(self, shapes, center = (0, 0), absolute=False):
        """ Convert shapes """
        result = []

        def fix_point(point):
            x, y = (point[0] + center[0], point[1] + center[1])
            if absolute:
                # freerouter often creates points outside boundary, fix it
                if x > self.max_x:
                    x = self.min_x + x - self.max_x
                elif x < self.min_x:
                    x = self.max_x - x - self.min_x
                if y > self.max_y:
                    y = self.min_y + y - self.max_y
                elif y < self.min_y:
                    y = self.max_y - y - self.min_y

            return (x, y)


        for shape in shapes:
            if isinstance(shape, specctraobj.PolylinePath):
                points = [fix_point(self.to_pixels(point)) for point in shape.vertex]
                result.extend(self._convert_path(self.to_pixels(shape.aperture_width), points))

            elif isinstance(shape, specctraobj.Path):
                points = [fix_point(self.to_pixels(point)) for point in shape.vertex]
                # Path has connected start and end points
                if points[0] != points[-1] and len(points) != 2:
                    points.append(points[0])
                result.extend(self._convert_path(self.to_pixels(shape.aperture_width), points))

            elif isinstance(shape, specctraobj.Polygon):
                points = [fix_point(self.to_pixels(point)) for point in shape.vertex]
                points = [Point(point[0], point[1]) for point in points]
                result.append(Polygon(points))

            elif isinstance(shape, specctraobj.Rectangle):
                x1, y1 = self.to_pixels(shape.vertex1)
                x2, y2 = self.to_pixels(shape.vertex2)
                width, height = abs(x1 - x2), abs(y1 - y2)
                x1, y1 = fix_point((min(x1, x2), max(y1, y2)))

                result.append(Rectangle(x1, y1, width, height))
            elif isinstance(shape, specctraobj.Circle):
                point = fix_point(self.to_pixels(shape.vertex))
                result.append(Circle(point[0], point[1], self.to_pixels(shape.diameter / 2.0)))
        return result

    def to_pixels(self, vertex):
        return self.resolution.to_pixels(vertex)

    def walk(self, elem):
        if isinstance(elem, list) and len(elem) > 0:
            elemx = [self.walk(x) for x in elem]
            func = specctraobj.lookup(elemx[0])
            if func:
                f = func()
                f.parse(elemx[1:])
                return f
            else:
#print 'Unhandled element', elemx[0]
                return elemx
        else:
            return elem

def to_piradians(degrees):
    # looks like specctra and upverter rotate in different directions
    return float(degrees) / 180.0

def rotate(point, piradians):
    """ Rotate point around (0, 0) """
    x, y = float(point[0]), float(point[1])
    # Somehow this must rotate in opposite direction than shape, why?
    radians = float(-piradians) * math.pi
    new_x = int(round(x * math.cos(radians) - y * math.sin(radians)))
    new_y = int(round(x * math.sin(radians) + y * math.cos(radians)))
    return (new_x, new_y)

class DsnParser:
    """ Parser for Specctra dialect of lisp """

    # Specctra parser configuration: Disables parentheses as delimiters for text strings.
    string_quote = ''
    # Specctra parser configuration: By default, blank spaces are an absolute delimiter. 
    space_in_quoted_tokens = False

    seperators = whitespace + '()'

    def __init__(self):
        self.pos = 0
        self.length = 0
        self.exp = ''

    def parse(self, exp):
        """ Parses s-expressions and returns them as Python lists """
        self.pos = 0
        self.length = len(exp)
        self.exp = exp
        return self._parse_exp(root=True)[0]

    def _maybe_eval(self, lst):
        """ File format specifies string quoting character:
        this eval configures parser so it can distinguish between
        quote character as atom and quoted string """

        if len(lst) > 1:
            if lst[0] == 'string_quote':
                self.string_quote = lst[1]
            elif lst[0] == 'space_in_quoted_tokens':
                self.space_in_quoted_tokens = lst[1].lower() == 'on'
        return lst

    def _parse_exp(self, root=False):
        """ Parses s-expressions and returns them as Python lists """

        lst = []
        buf = ''

        while self.pos < self.length:
            ch = self.exp[self.pos]
            self.pos += 1

            if ch not in self.seperators and ch != self.string_quote:
                buf += ch
            else:
                if buf and ch != self.string_quote:
                    lst.append(buf)
                    buf = ''

                if ch == '(':
                    lst.append(self._maybe_eval(self._parse_exp()))
                elif ch == ')':
                    return lst
                elif ch == self.string_quote:
                    buf += self._parse_string()

        if not root:
            raise SyntaxError('Closing ) not found')
        return lst

    def _parse_string(self):
        """ Reads string from expression according to current parser configuration """

        buf = ''

        while self.pos < self.length:
            ch = self.exp[self.pos]
            self.pos += 1

            if ch == self.string_quote:
                return buf
            elif ch in whitespace and not self.space_in_quoted_tokens:
                self.pos -= 1
                return buf
            else:
                buf += ch

        raise SyntaxError('Closing string quote %s not found' % (self.string_quote))
